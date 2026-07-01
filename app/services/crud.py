"""
app/services/crud.py
---------------------
All database read/write operations.
Keeps SQL queries out of route handlers and services.
Every function takes a db: AsyncSession parameter - never creates its own session.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.models import (
    User, DriverProfile, Ride, Delivery, Message, DriverStrike,
    MessageRole, MessageIntent, RideStatus, DeliveryStatus,
    StrikeReason, DriverAvailability
)

logger = get_logger(__name__)


# ── Users ──────────────────────────────────────────────────────────────────────

async def get_user_by_phone(phone_number: str, db: AsyncSession) -> Optional[User]:
    result = await db.execute(select(User).where(User.phone_number == phone_number))
    return result.scalar_one_or_none()


async def get_user_by_id(user_id: UUID, db: AsyncSession) -> Optional[User]:
    return await db.get(User, user_id)


async def create_user(
    phone_number: str,
    name: str,
    role: str,
    db: AsyncSession,
) -> User:
    user = User(
        id=uuid.uuid4(),
        phone_number=phone_number,
        name=name,
        role=role,
        is_active=True,
        is_verified=False,
        wallet_balance_ugx=0,
    )
    db.add(user)
    await db.flush()   # flush to get the ID without committing
    return user


# ── Messages ───────────────────────────────────────────────────────────────────

async def save_message(
    db: AsyncSession,
    user_id: UUID,
    role: MessageRole,
    content: str,
    intent: MessageIntent = MessageIntent.NONE,
    ride_id: Optional[UUID] = None,
    delivery_id: Optional[UUID] = None,
) -> Message:
    message = Message(
        id=uuid.uuid4(),
        user_id=user_id,
        role=role,
        content=content,
        intent=intent,
        ride_id=ride_id,
        delivery_id=delivery_id,
    )
    db.add(message)
    await db.flush()
    return message


# ── Rides ──────────────────────────────────────────────────────────────────────

async def create_ride(
    db: AsyncSession,
    passenger_id: UUID,
    pickup_name: str,
    dropoff_name: str,
    pickup_lat: float,
    pickup_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
    estimated_distance_km: float,
    estimated_duration_minutes: float,
    estimated_fare_ugx: int,
) -> Ride:
    ride = Ride(
        id=uuid.uuid4(),
        passenger_id=passenger_id,
        pickup_location=f"SRID=4326;POINT({pickup_lon} {pickup_lat})",
        dropoff_location=f"SRID=4326;POINT({dropoff_lon} {dropoff_lat})",
        pickup_name=pickup_name,
        dropoff_name=dropoff_name,
        estimated_distance_km=estimated_distance_km,
        estimated_duration_minutes=estimated_duration_minutes,
        estimated_fare_ugx=estimated_fare_ugx,
        status=RideStatus.REQUESTED,
    )
    db.add(ride)
    await db.flush()
    return ride


async def get_active_ride_for_passenger(
    user_id: UUID,
    db: AsyncSession,
) -> Optional[Ride]:
    result = await db.execute(
        select(Ride).where(
            and_(
                Ride.passenger_id == user_id,
                Ride.status.in_([
                    RideStatus.REQUESTED,
                    RideStatus.MATCHED,
                    RideStatus.ACCEPTED,
                    RideStatus.DRIVER_ARRIVING,
                    RideStatus.IN_PROGRESS,
                ])
            )
        )
        .order_by(Ride.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_ride_for_driver(
    user_id: UUID,
    db: AsyncSession,
) -> Optional[Ride]:
    """
    A ride only carries a driver_id once it has been matched, so REQUESTED
    is intentionally excluded here (unlike get_active_ride_for_passenger).
    """
    result = await db.execute(
        select(Ride).where(
            and_(
                Ride.driver_id == user_id,
                Ride.status.in_([
                    RideStatus.MATCHED,
                    RideStatus.ACCEPTED,
                    RideStatus.DRIVER_ARRIVING,
                    RideStatus.IN_PROGRESS,
                ])
            )
        )
        .order_by(Ride.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def cancel_ride(
    db: AsyncSession,
    ride_id: UUID,
    reason: str,
) -> None:
    await db.execute(
        update(Ride)
        .where(Ride.id == ride_id)
        .values(
            status=RideStatus.CANCELLED,
            cancelled_at=datetime.now(timezone.utc),
            cancellation_reason=reason,
        )
    )


# ── Deliveries ─────────────────────────────────────────────────────────────────

async def create_delivery(
    db: AsyncSession,
    passenger_id: UUID,
    pickup_name: str,
    dropoff_name: str,
    item_description: str,
    is_urgent: bool,
) -> Delivery:
    delivery = Delivery(
        id=uuid.uuid4(),
        passenger_id=passenger_id,
        pickup_name=pickup_name,
        dropoff_name=dropoff_name,
        item_description=item_description,
        is_urgent=is_urgent,
        status=DeliveryStatus.PENDING,
    )
    db.add(delivery)
    await db.flush()
    return delivery


async def get_active_delivery_for_passenger(
    user_id: UUID,
    db: AsyncSession,
) -> Optional[Delivery]:
    result = await db.execute(
        select(Delivery).where(
            and_(
                Delivery.passenger_id == user_id,
                Delivery.status.in_([
                    DeliveryStatus.PENDING,
                    DeliveryStatus.UNDER_REVIEW,
                    DeliveryStatus.NEGOTIATING,
                    DeliveryStatus.CONFIRMED,
                    DeliveryStatus.ASSIGNED,
                    DeliveryStatus.IN_PROGRESS,
                ])
            )
        )
        .order_by(Delivery.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Driver Strikes ─────────────────────────────────────────────────────────────

async def issue_driver_strike(
    db: AsyncSession,
    driver_user_id: UUID,
    reason: str,
    notes: str = "",
    admin_id: Optional[UUID] = None,
    ride_id: Optional[UUID] = None,
) -> DriverStrike:
    """
    Issue a strike and check if the driver should be auto-suspended.
    Auto-suspension threshold: 3 active strikes in 30 days.
    """
    from datetime import timedelta
    from sqlalchemy import func

    driver_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == driver_user_id)
    )
    driver = driver_result.scalar_one_or_none()
    if not driver:
        logger.error("strike_driver_not_found", driver_user_id=str(driver_user_id))
        return

    strike = DriverStrike(
        id=uuid.uuid4(),
        driver_id=driver.id,
        issued_by_admin_id=admin_id,
        ride_id=ride_id,
        reason=StrikeReason(reason),
        notes=notes,
        is_active=True,
    )
    db.add(strike)
    await db.flush()

    # Count active strikes in the last 30 days
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    count_result = await db.execute(
        select(func.count(DriverStrike.id)).where(
            and_(
                DriverStrike.driver_id == driver.id,
                DriverStrike.is_active == True,
                DriverStrike.created_at >= thirty_days_ago,
            )
        )
    )
    active_strikes = count_result.scalar() or 0

    if active_strikes >= 3:
        # Auto-suspend
        await db.execute(
            update(DriverProfile)
            .where(DriverProfile.id == driver.id)
            .values(
                availability=DriverAvailability.OFFLINE,
                subscription_active=False,
            )
        )
        await db.execute(
            update(User)
            .where(User.id == driver_user_id)
            .values(is_active=False)
        )
        logger.warning("driver_auto_suspended", driver_id=str(driver.id), strikes=active_strikes)

    return strike
