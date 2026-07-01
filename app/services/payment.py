"""
app/services/payment.py
------------------------
PesaPal payment integration for the Kabale Transport Platform.

Handles:
1. Wallet top-up (passenger loads money via MTN MoMo or Airtel Money)
2. Ride payment split (automatic commission deduction on completion)
3. Driver withdrawal (driver pulls earnings to mobile money)
4. IPN (Instant Payment Notification) webhook processing

PesaPal v3 flow:
  1. Get OAuth token (expires in 5 minutes - cached in Redis)
  2. Register IPN URL (once per deployment)
  3. Submit order -> get redirect URL -> user completes payment on PesaPal
  4. PesaPal calls your IPN URL -> you verify -> credit wallet

Security:
  - All financial operations use database transactions
  - Wallet balance stored as integer UGX (no floating point)
  - Every money movement creates an immutable Transaction record
  - IPN callbacks are verified before any money is credited
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import (
    User, Ride, Transaction, RideStatus,
    TransactionType, TransactionStatus
)
from app.services.cache import get_redis

logger = get_logger(__name__)

# Redis key for cached PesaPal token
PESAPAL_TOKEN_KEY = "pesapal:access_token"
PESAPAL_TOKEN_TTL = 270   # 4.5 minutes (token valid 5 min, refresh before expiry)


class PaymentError(Exception):
    pass


class PesaPalService:

    def __init__(self):
        self.base_url = settings.PESAPAL_BASE_URL
        self.consumer_key = settings.PESAPAL_CONSUMER_KEY
        self.consumer_secret = settings.PESAPAL_CONSUMER_SECRET

    # ── Authentication ─────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        """
        Get a valid PesaPal OAuth token.
        Cached in Redis - refreshed automatically before expiry.
        """
        redis = await get_redis()
        cached = await redis.get(PESAPAL_TOKEN_KEY)
        if cached:
            return cached

        payload = {
            "consumer_key": self.consumer_key,
            "consumer_secret": self.consumer_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/Auth/RequestToken",
                    json=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                response.raise_for_status()
                data = response.json()

            token = data["token"]
            await redis.setex(PESAPAL_TOKEN_KEY, PESAPAL_TOKEN_TTL, token)

            logger.info("pesapal_token_refreshed")
            return token

        except httpx.HTTPError as e:
            logger.error("pesapal_auth_error", error=str(e))
            raise PaymentError(f"PesaPal authentication failed: {str(e)}")

    # ── IPN Registration ───────────────────────────────────────────────────────

    async def register_ipn(self) -> str:
        """
        Register the platform's IPN URL with PesaPal.
        Called once at application startup.
        Returns the IPN ID to use in all subsequent orders.
        Cached in Redis permanently.
        """
        redis = await get_redis()
        cached_ipn_id = await redis.get("pesapal:ipn_id")
        if cached_ipn_id:
            return cached_ipn_id

        token = await self._get_token()

        payload = {
            "url": settings.PESAPAL_IPN_URL,
            "ipn_notification_type": "GET",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/api/URLSetup/RegisterIPN",
                json=payload,
                headers=self._auth_headers(token),
            )
            response.raise_for_status()
            data = response.json()

        ipn_id = data["ipn_id"]
        await redis.set("pesapal:ipn_id", ipn_id)

        logger.info("pesapal_ipn_registered", ipn_id=ipn_id)
        return ipn_id

    # ── Wallet Top-up ──────────────────────────────────────────────────────────

    async def initiate_wallet_topup(
        self,
        user: User,
        amount_ugx: int,
        db: AsyncSession,
    ) -> dict:
        """
        Initiate a wallet top-up via PesaPal.
        Returns a redirect URL that the passenger completes in a webview.

        Flow:
        1. Create a pending Transaction record
        2. Submit order to PesaPal
        3. Return redirect URL to mobile app
        4. Passenger completes payment on PesaPal (MTN MoMo or Airtel Money)
        5. PesaPal calls our IPN endpoint
        6. We verify and credit the wallet
        """
        if amount_ugx < 1000:
            raise PaymentError("Minimum top-up amount is 1,000 UGX")

        token = await self._get_token()
        ipn_id = await self.register_ipn()

        order_id = str(uuid.uuid4())

        # 1. Create pending transaction record BEFORE calling PesaPal
        # This ensures we have a record even if PesaPal call fails
        transaction = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            type=TransactionType.WALLET_TOPUP,
            status=TransactionStatus.PENDING,
            amount_ugx=amount_ugx,
            pesapal_order_id=order_id,
            description=f"Wallet top-up of {amount_ugx:,} UGX",
        )
        db.add(transaction)
        await db.commit()

        # 2. Submit order to PesaPal
        # Parse phone for billing info (format: 256XXXXXXXXX)
        phone = user.phone_number.lstrip("+")

        payload = {
            "id": order_id,
            "currency": "UGX",
            "amount": amount_ugx,
            "description": "Kabale Transport - Wallet Top-up",
            "callback_url": settings.PESAPAL_CALLBACK_URL,
            "notification_id": ipn_id,
            "billing_address": {
                "phone_number": phone,
                "first_name": user.name.split()[0],
                "last_name": " ".join(user.name.split()[1:]) or user.name.split()[0],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/Transactions/SubmitOrderRequest",
                    json=payload,
                    headers=self._auth_headers(token),
                )
                response.raise_for_status()
                data = response.json()

            redirect_url = data["redirect_url"]
            pesapal_tracking_id = data.get("order_tracking_id", "")

            # Update transaction with PesaPal tracking ID
            await db.execute(
                update(Transaction)
                .where(Transaction.id == transaction.id)
                .values(pesapal_tracking_id=pesapal_tracking_id)
            )
            await db.commit()

            logger.info(
                "topup_order_submitted",
                user_id=str(user.id),
                amount_ugx=amount_ugx,
                order_id=order_id,
            )

            return {
                "order_id": order_id,
                "redirect_url": redirect_url,
                "amount_ugx": amount_ugx,
            }

        except httpx.HTTPError as e:
            logger.error("pesapal_order_submit_error", error=str(e))
            # Mark transaction as failed
            await db.execute(
                update(Transaction)
                .where(Transaction.id == transaction.id)
                .values(status=TransactionStatus.FAILED)
            )
            await db.commit()
            raise PaymentError(f"Failed to initiate payment: {str(e)}")

    # ── IPN Callback Processing ────────────────────────────────────────────────

    async def process_ipn_callback(
        self,
        order_tracking_id: str,
        order_merchant_reference: str,
        db: AsyncSession,
    ) -> bool:
        """
        Process a PesaPal IPN (Instant Payment Notification) callback.

        PesaPal calls this endpoint after payment is completed or fails.
        We verify the payment status with PesaPal directly (don't trust the callback alone)
        and then credit the wallet.

        Returns True if payment was successful and wallet was credited.
        """
        # 1. Verify payment status directly with PesaPal (don't trust callback params alone)
        token = await self._get_token()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/api/Transactions/GetTransactionStatus",
                    params={"orderTrackingId": order_tracking_id},
                    headers=self._auth_headers(token),
                )
                response.raise_for_status()
                status_data = response.json()

        except httpx.HTTPError as e:
            logger.error("pesapal_status_check_error", error=str(e))
            raise PaymentError("Cannot verify payment status with PesaPal")

        payment_status = status_data.get("payment_status_description", "").lower()
        amount = status_data.get("amount", 0)

        logger.info(
            "pesapal_ipn_received",
            order_tracking_id=order_tracking_id,
            merchant_ref=order_merchant_reference,
            status=payment_status,
        )

        if payment_status != "completed":
            # Payment failed or is still pending
            await db.execute(
                update(Transaction)
                .where(Transaction.pesapal_order_id == order_merchant_reference)
                .values(status=TransactionStatus.FAILED)
            )
            await db.commit()
            return False

        # 2. Find the pending transaction
        result = await db.execute(
            select(Transaction).where(
                Transaction.pesapal_order_id == order_merchant_reference,
                Transaction.status == TransactionStatus.PENDING,
            )
        )
        transaction = result.scalar_one_or_none()

        if not transaction:
            # Could be a duplicate IPN - already processed
            logger.warning("ipn_no_pending_transaction", order_id=order_merchant_reference)
            return False

        # 3. Credit wallet using a database transaction (atomic)
        try:
            # Get current balance
            user_result = await db.execute(
                select(User).where(User.id == transaction.user_id)
            )
            user = user_result.scalar_one()

            new_balance = user.wallet_balance_ugx + transaction.amount_ugx

            # Update wallet balance
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(wallet_balance_ugx=new_balance)
            )

            # Mark transaction as completed with balance snapshot
            await db.execute(
                update(Transaction)
                .where(Transaction.id == transaction.id)
                .values(
                    status=TransactionStatus.COMPLETED,
                    pesapal_tracking_id=order_tracking_id,
                    balance_after_ugx=new_balance,
                    settled_at=datetime.now(timezone.utc),
                )
            )

            await db.commit()

            # Notify passenger
            from app.services.notifications import notification_service
            await notification_service.notify_passenger_wallet_credited(
                user_id=user.id,
                amount_ugx=transaction.amount_ugx,
                db=db,
            )

            logger.info(
                "wallet_credited",
                user_id=str(user.id),
                amount_ugx=transaction.amount_ugx,
                new_balance=new_balance,
            )

            return True

        except Exception as e:
            await db.rollback()
            logger.error("wallet_credit_failed", error=str(e), transaction_id=str(transaction.id))
            raise PaymentError(f"Wallet credit failed: {str(e)}")

    # ── Ride Payment Split ─────────────────────────────────────────────────────

    async def process_ride_payment(
        self,
        ride_id: UUID,
        db: AsyncSession,
    ) -> None:
        """
        Process automatic payment split when a ride is completed.

        Operations (all atomic - rolled back together if any fails):
        1. Debit passenger wallet by final fare
        2. Credit driver wallet with driver earnings
        3. Record commission transaction for the platform
        4. Update ride status to PAID
        5. Update driver's earnings total
        """
        ride = await db.get(Ride, ride_id)
        if not ride or ride.status != RideStatus.COMPLETED:
            raise PaymentError(f"Ride {ride_id} is not in COMPLETED state")

        if not ride.final_fare_ugx or not ride.commission_ugx or not ride.driver_earnings_ugx:
            raise PaymentError(f"Ride {ride_id} has incomplete fare data")

        # Load passenger and driver
        passenger_result = await db.execute(select(User).where(User.id == ride.passenger_id))
        passenger = passenger_result.scalar_one()

        driver_result = await db.execute(select(User).where(User.id == ride.driver_id))
        driver = driver_result.scalar_one()

        # Check passenger has sufficient balance
        if passenger.wallet_balance_ugx < ride.final_fare_ugx:
            logger.error(
                "insufficient_wallet_balance",
                user_id=str(passenger.id),
                balance=passenger.wallet_balance_ugx,
                required=ride.final_fare_ugx,
            )
            # In production: trigger a recovery flow or allow debt up to a threshold
            raise PaymentError("Insufficient wallet balance")

        now = datetime.now(timezone.utc)

        try:
            # 1. Debit passenger
            new_passenger_balance = passenger.wallet_balance_ugx - ride.final_fare_ugx
            await db.execute(
                update(User)
                .where(User.id == passenger.id)
                .values(wallet_balance_ugx=new_passenger_balance)
            )

            # 2. Credit driver
            new_driver_balance = driver.wallet_balance_ugx + ride.driver_earnings_ugx
            await db.execute(
                update(User)
                .where(User.id == driver.id)
                .values(wallet_balance_ugx=new_driver_balance)
            )

            # 3. Record passenger debit transaction
            passenger_txn = Transaction(
                user_id=passenger.id,
                type=TransactionType.RIDE_PAYMENT,
                status=TransactionStatus.COMPLETED,
                amount_ugx=ride.final_fare_ugx,
                ride_id=ride.id,
                description=f"Ride payment - {ride.pickup_name} to {ride.dropoff_name}",
                balance_after_ugx=new_passenger_balance,
                settled_at=now,
            )
            db.add(passenger_txn)

            # 4. Record driver credit transaction
            driver_txn = Transaction(
                user_id=driver.id,
                type=TransactionType.DRIVER_CREDIT,
                status=TransactionStatus.COMPLETED,
                amount_ugx=ride.driver_earnings_ugx,
                ride_id=ride.id,
                description=f"Ride earnings - {ride.pickup_name} to {ride.dropoff_name}",
                balance_after_ugx=new_driver_balance,
                settled_at=now,
            )
            db.add(driver_txn)

            # 5. Record platform commission (against driver account for tracking)
            commission_txn = Transaction(
                user_id=driver.id,
                type=TransactionType.COMMISSION,
                status=TransactionStatus.COMPLETED,
                amount_ugx=ride.commission_ugx,
                ride_id=ride.id,
                description=f"Platform commission ({settings.PRICING_COMMISSION_PERCENT}%)",
                balance_after_ugx=new_driver_balance,
                settled_at=now,
            )
            db.add(commission_txn)

            # 6. Mark ride as PAID
            await db.execute(
                update(Ride)
                .where(Ride.id == ride_id)
                .values(status=RideStatus.PAID, paid_at=now)
            )

            await db.commit()

            logger.info(
                "ride_payment_processed",
                ride_id=str(ride_id),
                fare=ride.final_fare_ugx,
                commission=ride.commission_ugx,
                driver_earnings=ride.driver_earnings_ugx,
            )

        except Exception as e:
            await db.rollback()
            logger.error("ride_payment_failed", ride_id=str(ride_id), error=str(e))
            raise PaymentError(f"Payment processing failed: {str(e)}")

    # ── Driver Withdrawal ──────────────────────────────────────────────────────

    async def initiate_driver_withdrawal(
        self,
        driver: User,
        amount_ugx: int,
        phone_number: str,
        db: AsyncSession,
    ) -> dict:
        """
        Driver requests a withdrawal of their earnings to mobile money.
        Uses PesaPal's disbursement API.

        Minimum withdrawal: 5,000 UGX
        Phone must be MTN MoMo or Airtel Money number in Uganda.
        """
        if amount_ugx < 5000:
            raise PaymentError("Minimum withdrawal amount is 5,000 UGX")

        if driver.wallet_balance_ugx < amount_ugx:
            raise PaymentError(
                f"Insufficient balance. Available: {driver.wallet_balance_ugx:,} UGX"
            )

        token = await self._get_token()

        # Pre-debit wallet (hold the amount)
        new_balance = driver.wallet_balance_ugx - amount_ugx
        await db.execute(
            update(User)
            .where(User.id == driver.id)
            .values(wallet_balance_ugx=new_balance)
        )

        # Create pending withdrawal transaction
        withdrawal_txn = Transaction(
            user_id=driver.id,
            type=TransactionType.WITHDRAWAL,
            status=TransactionStatus.PENDING,
            amount_ugx=amount_ugx,
            description=f"Withdrawal to {phone_number}",
            balance_after_ugx=new_balance,
        )
        db.add(withdrawal_txn)
        await db.commit()

        # Submit to PesaPal mobile money payout API
        payload = {
            "amount": amount_ugx,
            "currency": "UGX",
            "phone_number": phone_number.lstrip("+"),
            "description": "Kabale Transport - Driver Earnings",
            "reference": str(withdrawal_txn.id),
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/Transactions/MobileMoneySend",
                    json=payload,
                    headers=self._auth_headers(token),
                )
                response.raise_for_status()
                data = response.json()

            # Mark as completed (payout initiated)
            await db.execute(
                update(Transaction)
                .where(Transaction.id == withdrawal_txn.id)
                .values(
                    status=TransactionStatus.COMPLETED,
                    pesapal_tracking_id=data.get("tracking_id"),
                    settled_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

            logger.info(
                "driver_withdrawal_initiated",
                driver_id=str(driver.id),
                amount_ugx=amount_ugx,
                phone=phone_number,
            )

            return {
                "status": "initiated",
                "amount_ugx": amount_ugx,
                "new_balance_ugx": new_balance,
            }

        except httpx.HTTPError as e:
            # Reverse the wallet deduction on failure
            await db.execute(
                update(User)
                .where(User.id == driver.id)
                .values(wallet_balance_ugx=driver.wallet_balance_ugx)
            )
            await db.execute(
                update(Transaction)
                .where(Transaction.id == withdrawal_txn.id)
                .values(status=TransactionStatus.FAILED)
            )
            await db.commit()
            logger.error("driver_withdrawal_failed", error=str(e))
            raise PaymentError(f"Withdrawal failed: {str(e)}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _auth_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


# ── Singleton ──────────────────────────────────────────────────────────────────
payment_service = PesaPalService()
