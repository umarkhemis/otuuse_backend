"""
app/tasks/dispatch_tasks.py
----------------------------
Celery async tasks for dispatch operations that need to run in the background.

- check_driver_acceptance_timeout: fires when driver doesn't respond to ride alert
- auto_complete_ride: fires when driver is stationary at destination
- check_subscription_expiry: daily job to deactivate expired subscriptions
- update_driver_ratings: recalculates driver ratings after each ride
"""

import asyncio
from uuid import UUID

from celery import Celery

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Celery App ─────────────────────────────────────────────────────────────────
def _celery_url(url: str) -> str:
    """Celery requires ssl_cert_reqs param for rediss:// URLs (Upstash)."""
    if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}ssl_cert_reqs=CERT_NONE"
    return url

celery_app = Celery(
    "kabale_transport",
    broker=_celery_url(settings.REDIS_URL),
    backend=_celery_url(settings.REDIS_URL),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Africa/Kampala",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # ack only after task completes (safer)
    worker_prefetch_multiplier=1,  # one task at a time per worker
)

# ── Scheduled Tasks (Beat) ─────────────────────────────────────────────────────
celery_app.conf.beat_schedule = {
    "check-subscription-expiry-daily": {
        "task": "app.tasks.dispatch_tasks.check_subscription_expiry",
        "schedule": 86400.0,  # every 24 hours
    },
}


def run_async(coro):
    """Helper to run async code inside Celery tasks (sync context)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.dispatch_tasks.check_driver_acceptance_timeout", bind=True, max_retries=0)
def check_driver_acceptance_timeout(self, ride_id_str: str):
    """
    Fires DISPATCH_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS after a ride is dispatched.
    If the driver still hasn't accepted, treat it as a decline and reassign.
    """
    async def _run():
        from app.services.cache import ride_acceptance_tracker
        from app.db.session import get_db_context
        from app.services.dispatch import dispatch_service

        pending_driver = await ride_acceptance_tracker.get_pending_driver(ride_id_str)

        if not pending_driver:
            # Driver already responded - nothing to do
            logger.info("acceptance_timeout_not_needed", ride_id=ride_id_str)
            return

        logger.warning("driver_acceptance_timeout", ride_id=ride_id_str, driver_id=pending_driver)

        async with get_db_context() as db:
            await dispatch_service.handle_driver_decline(
                ride_id=UUID(ride_id_str),
                driver_user_id=UUID(pending_driver),
                db=db,
            )

        # Issue a no-show strike
        async with get_db_context() as db:
            from app.services import crud
            await crud.issue_driver_strike(
                db=db,
                driver_user_id=UUID(pending_driver),
                reason="no_show",
                notes=f"No response to ride {ride_id_str} within acceptance window",
            )

    run_async(_run())


@celery_app.task(name="app.tasks.dispatch_tasks.auto_complete_ride", bind=True, max_retries=0)
def auto_complete_ride(self, ride_id_str: str):
    """
    Fires after driver has been stationary at destination for the configured delay.
    Auto-completes the ride if driver hasn't already tapped Complete Ride.
    """
    async def _run():
        from app.db.session import get_db_context
        from app.services.dispatch import dispatch_service
        from app.models.models import Ride, RideStatus
        from sqlalchemy import select

        async with get_db_context() as db:
            result = await db.execute(
                select(Ride).where(Ride.id == UUID(ride_id_str))
            )
            ride = result.scalar_one_or_none()

            if not ride:
                return

            if ride.status == RideStatus.COMPLETED:
                # Driver already tapped Complete - nothing to do
                logger.info("auto_complete_not_needed", ride_id=ride_id_str)
                return

            if ride.status == RideStatus.IN_PROGRESS:
                logger.info("auto_completing_ride", ride_id=ride_id_str)
                await dispatch_service.complete_ride(ride_id=UUID(ride_id_str), db=db)

    run_async(_run())


@celery_app.task(name="app.tasks.dispatch_tasks.check_subscription_expiry")
def check_subscription_expiry():
    """
    Runs daily. Deactivates drivers whose subscription has expired.
    They will be invisible to dispatch until they renew.
    """
    async def _run():
        from datetime import datetime, timezone
        from sqlalchemy import update, select, and_
        from app.db.session import get_db_context
        from app.models.models import DriverProfile, DriverAvailability

        now = datetime.now(timezone.utc)

        async with get_db_context() as db:
            result = await db.execute(
                update(DriverProfile)
                .where(
                    and_(
                        DriverProfile.subscription_active == True,
                        DriverProfile.subscription_expires_at < now,
                    )
                )
                .values(
                    subscription_active=False,
                    availability=DriverAvailability.OFFLINE,
                )
                .returning(DriverProfile.id)
            )
            expired_count = len(result.fetchall())
            await db.commit()

            if expired_count > 0:
                logger.warning("subscriptions_expired", count=expired_count)

    run_async(_run())


@celery_app.task(name="app.tasks.dispatch_tasks.recalculate_driver_rating")
def recalculate_driver_rating(driver_user_id_str: str):
    """
    Recalculates a driver's average rating after a new rating is submitted.
    Uses the last 50 ratings for a rolling average (prevents old rides
    permanently damaging a driver's score).
    """
    async def _run():
        from sqlalchemy import select, func
        from app.db.session import get_db_context
        from app.models.models import Ride, DriverProfile, RideStatus
        from sqlalchemy import update

        async with get_db_context() as db:
            driver_user_id = UUID(driver_user_id_str)

            result = await db.execute(
                select(func.avg(Ride.passenger_rating))
                .where(
                    Ride.driver_id == driver_user_id,
                    Ride.passenger_rating.isnot(None),
                    Ride.status == RideStatus.COMPLETED,
                )
                .order_by(Ride.completed_at.desc())
                .limit(50)
            )
            avg_rating = result.scalar() or 5.0

            await db.execute(
                update(DriverProfile)
                .where(DriverProfile.user_id == driver_user_id)
                .values(rating=round(float(avg_rating), 2))
            )
            await db.commit()

            logger.info("driver_rating_updated", driver=driver_user_id_str, new_rating=avg_rating)

    run_async(_run())


@celery_app.task(name="app.tasks.dispatch_tasks.remind_admins_delivery", bind=True, max_retries=3)
def remind_admins_delivery(self, delivery_id: str):
    """
    Re-notifies all admins about a pending delivery request that hasn't
    been replied to yet. Called 5 minutes after delivery creation, and
    retries every 5 minutes up to 3 times (15 minutes total).
    """
    async def _run():
        from app.db.session import get_standalone_db_session
        from app.models.models import Delivery, DeliveryStatus, MessageRole, Message
        from sqlalchemy import select, desc

        async with get_standalone_db_session() as db:
            delivery = await db.get(Delivery, uuid.UUID(delivery_id))
            if not delivery:
                return  # Delivery no longer exists

            if delivery.status != DeliveryStatus.PENDING:
                return  # Already handled by an admin

            # Check if any admin has replied
            result = await db.execute(
                select(Message)
                .where(
                    Message.delivery_id == delivery.id,
                    Message.role.in_([MessageRole.ADMIN, MessageRole.AGENT]),
                )
                .limit(1)
            )
            if result.scalar_one_or_none():
                return  # Admin already replied

            # Still no reply - resend FCM to all admins
            from app.services.notifications import notification_service
            await notification_service.notify_admin_new_delivery(
                delivery_id=delivery.id, db=db
            )
            logger.info("admin_delivery_reminder_sent", delivery_id=delivery_id)

            # Schedule another reminder in 5 minutes
            raise self.retry(countdown=300)

    _run_async(_run())
