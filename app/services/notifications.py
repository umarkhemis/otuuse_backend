"""
app/services/notifications.py
-------------------------------
Firebase Cloud Messaging (FCM) push notification service.
Sends alerts to drivers and passengers via their registered device tokens.
"""
import json
import os
from typing import Optional
from uuid import UUID

import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import PushToken, User, Ride, Delivery

logger = get_logger(__name__)

# ── Firebase Initialization ────────────────────────────────────────────────────

_firebase_initialized = False


# def init_firebase():
#     """
#     Initialise Firebase Admin SDK.

#     Two credential strategies, tried in order:
#     1. FIREBASE_CREDENTIALS_JSON env var  - used on Render and other cloud
#        hosts where we can't guarantee a writable filesystem or commit secrets.
#        Set this to the full contents of your service-account JSON file.
#     2. FIREBASE_CREDENTIALS_PATH setting  - used locally (defaults to
#        'firebase-service-account.json' in the project root).
#     """
#     global _firebase_initialized
#     if not _firebase_initialized:
#         try:
#             creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
#             if creds_json:
#                 cred = credentials.Certificate(json.loads(creds_json))
#             else:
#                 cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
#             firebase_admin.initialize_app(cred)
#             _firebase_initialized = True
#             logger.info("firebase_initialized")
#         except Exception as e:
#             logger.error("firebase_init_failed", error=str(e))


def init_firebase():
    global _firebase_initialized
    if not _firebase_initialized:
        try:
            creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
            if creds_json:
                cred = credentials.Certificate(json.loads(creds_json))
            else:
                # Render secret file mount point, falls back to local dev path
                secret_path = "/etc/secrets/firebase-service-account.json"
                cred_path = secret_path if os.path.exists(secret_path) else settings.FIREBASE_CREDENTIALS_PATH
                cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            _firebase_initialized = True
            logger.info("firebase_initialized")
        except Exception as e:
            logger.error("firebase_init_failed", error=str(e))


class NotificationService:

    async def _get_user_tokens(self, user_id: UUID, db: AsyncSession) -> list[str]:
        result = await db.execute(
            select(PushToken.fcm_token).where(
                PushToken.user_id == user_id,
                PushToken.is_active == True,
            )
        )
        return [row[0] for row in result.fetchall()]

    async def _send(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict = None,
    ) -> None:
        if not tokens:
            return
        try:
            message = messaging.MulticastMessage(
                tokens=tokens,
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
                android=messaging.AndroidConfig(priority="high"),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default", badge=1)
                    )
                ),
            )
            response = messaging.send_each_for_multicast(message)
            logger.debug(
                "fcm_sent",
                success=response.success_count,
                failed=response.failure_count,
            )
        except Exception as e:
            logger.error("fcm_send_error", error=str(e))

    # ── Driver Notifications ───────────────────────────────────────────────────

    async def notify_driver_new_ride(
        self, driver_user_id: UUID, ride: Ride, db: AsyncSession
    ):
        tokens = await self._get_user_tokens(driver_user_id, db)
        await self._send(
            tokens=tokens,
            title="New Ride Request",
            body=f"{ride.pickup_name} -> {ride.dropoff_name} | {ride.estimated_fare_ugx:,} UGX",
            data={
                "type": "new_ride",
                "ride_id": str(ride.id),
                "pickup_name": ride.pickup_name,
                "dropoff_name": ride.dropoff_name,
                "fare_ugx": ride.estimated_fare_ugx,
                "timeout_seconds": settings.DISPATCH_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS,
            },
        )

    async def notify_driver_ride_cancelled(self, driver_id: UUID, db: AsyncSession):
        tokens = await self._get_user_tokens(driver_id, db)
        await self._send(
            tokens=tokens,
            title="Ride Cancelled",
            body="The passenger has cancelled this ride request.",
            data={"type": "ride_cancelled"},
        )

    # ── Passenger Notifications ────────────────────────────────────────────────

    async def notify_passenger_driver_accepted(
        self, ride_id: UUID, db: AsyncSession
    ):
        ride = await db.get(Ride, ride_id)
        if not ride:
            return
        tokens = await self._get_user_tokens(ride.passenger_id, db)
        await self._send(
            tokens=tokens,
            title="Driver On the Way",
            body="Your driver has accepted the ride and is heading to you.",
            data={"type": "driver_accepted", "ride_id": str(ride_id)},
        )

    async def notify_passenger_driver_arrived(
        self, ride_id: UUID, db: AsyncSession
    ):
        ride = await db.get(Ride, ride_id)
        if not ride:
            return
        tokens = await self._get_user_tokens(ride.passenger_id, db)
        await self._send(
            tokens=tokens,
            title="Driver Has Arrived",
            body="Your driver is at the pickup point. Please head over.",
            data={"type": "driver_arrived", "ride_id": str(ride_id)},
        )

    async def notify_passenger_no_driver(self, ride_id: UUID, db: AsyncSession):
        ride = await db.get(Ride, ride_id)
        if not ride:
            return
        tokens = await self._get_user_tokens(ride.passenger_id, db)
        await self._send(
            tokens=tokens,
            title="No Drivers Available",
            body="We couldn't find a driver right now. Please try again in a few minutes.",
            data={"type": "no_driver", "ride_id": str(ride_id)},
        )

    async def notify_passenger_wallet_credited(
        self, user_id: UUID, amount_ugx: int, db: AsyncSession
    ):
        tokens = await self._get_user_tokens(user_id, db)
        await self._send(
            tokens=tokens,
            title="Wallet Topped Up",
            body=f"{amount_ugx:,} UGX has been added to your wallet.",
            data={"type": "wallet_credit", "amount_ugx": amount_ugx},
        )


    async def notify_driver_passenger_confirmed(
        self, driver_user_id: UUID, ride_id: UUID, db: AsyncSession
    ):
        """Tell the driver the passenger confirmed - proceed to pickup."""
        tokens = await self._get_user_tokens(driver_user_id, db)
        await self._send(
            tokens=tokens,
            title="Passenger Confirmed!",
            body="Your passenger confirmed the ride. Head to the pickup point now.",
            data={"type": "passenger_confirmed", "ride_id": str(ride_id)},
        )

    # ── Admin Notifications ────────────────────────────────────────────────────

    async def notify_admin_new_delivery(
        self, delivery_id: UUID, db: AsyncSession
    ):
        """Notify all admin users of a new delivery request."""
        from app.models.models import UserRole

        admin_result = await db.execute(
            select(User).where(
                User.role == UserRole.ADMIN, User.is_active == True
            )
        )
        admins = admin_result.scalars().all()
        for admin in admins:
            tokens = await self._get_user_tokens(admin.id, db)
            await self._send(
                tokens=tokens,
                title="New Delivery Request",
                body="A passenger has requested a delivery. Tap to review.",
                data={"type": "new_delivery", "delivery_id": str(delivery_id)},
            )


# ── Singleton ──────────────────────────────────────────────────────────────────
notification_service = NotificationService()
