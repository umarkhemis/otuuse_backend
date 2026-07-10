"""
app/services/dispatch.py
-------------------------
The ride dispatch system. Handles:

1. Driver matching - PostGIS proximity query ranked by distance, rating, fairness
2. Ride assignment and FCM alerts to drivers
3. GPS-based ride state machine (auto-transitions backed by real location data)
4. Reassignment logic when a driver declines or times out
5. Celery tasks for async timeout monitoring

This is the most operationally critical service. Every decision here
affects real people on real roads.
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from geoalchemy2.functions import ST_DWithin, ST_Distance, ST_GeogFromWKB, ST_MakePoint, ST_SetSRID
from sqlalchemy import select, func, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import (
    DriverProfile, User, Ride, GPSTrailPoint,
    RideStatus, DriverAvailability, StrikeReason
)
from app.services.cache import driver_location_store, ride_acceptance_tracker
from app.services.routing import routing_service

logger = get_logger(__name__)


class DispatchError(Exception):
    pass


class DispatchService:

    # ── Driver Availability Query ──────────────────────────────────────────────

    async def count_available_drivers(
        self,
        pickup_lat: float,
        pickup_lon: float,
        db: AsyncSession,
    ) -> int:
        """
        Count online drivers within search radius of a pickup point.
        Uses PostGIS ST_DWithin for fast geospatial filtering.
        """
        radius_meters = settings.DISPATCH_MAX_DRIVER_SEARCH_RADIUS_KM * 1000

        result = await db.execute(
            select(func.count(DriverProfile.id))
            .where(
                and_(
                    DriverProfile.availability == DriverAvailability.ONLINE,
                    DriverProfile.subscription_active == True,
                    ST_DWithin(
                        ST_GeogFromWKB(DriverProfile.current_location),
                        ST_GeogFromWKB(
                            ST_SetSRID(ST_MakePoint(pickup_lon, pickup_lat), 4326)
                        ),
                        radius_meters,
                    )
                )
            )
        )

        return result.scalar() or 0

    async def find_best_driver(
        self,
        pickup_lat: float,
        pickup_lon: float,
        db: AsyncSession,
        exclude_driver_ids: list[UUID] = None,
    ) -> Optional[DriverProfile]:
        """
        Find the best available driver using a composite ranking:

        Score = (1 / distance_km) * 0.5
              + (rating / 5.0) * 0.3
              + (1 / (rides_today + 1)) * 0.2

        - Distance: closest driver gets highest score (50% weight)
        - Rating: higher-rated drivers preferred (30% weight)
        - Fairness: drivers with fewer rides today preferred (20% weight)

        This prevents always assigning the same top driver and distributes
        work more fairly across the driver pool.

        Drivers in exclude_driver_ids are skipped (for reassignment flow).
        """
        radius_meters = settings.DISPATCH_MAX_DRIVER_SEARCH_RADIUS_KM * 1000
        exclude_ids = exclude_driver_ids or []

        pickup_point = ST_SetSRID(ST_MakePoint(pickup_lon, pickup_lat), 4326)

        result = await db.execute(
            select(
                DriverProfile,
                ST_Distance(
                    ST_GeogFromWKB(DriverProfile.current_location),
                    ST_GeogFromWKB(pickup_point),
                ).label("distance_meters"),
            )
            .where(
                and_(
                    DriverProfile.availability == DriverAvailability.ONLINE,
                    DriverProfile.subscription_active == True,
                    ~DriverProfile.id.in_(exclude_ids),
                    ST_DWithin(
                        ST_GeogFromWKB(DriverProfile.current_location),
                        ST_GeogFromWKB(pickup_point),
                        radius_meters,
                    )
                )
            )
            .order_by("distance_meters")
            .limit(10)   # score top 10 candidates
        )

        candidates = result.all()

        if not candidates:
            return None

        # Score each candidate
        def score(driver: DriverProfile, distance_meters: float) -> float:
            distance_km = max(distance_meters / 1000, 0.1)
            distance_score = (1 / distance_km) * 0.5
            rating_score = (driver.rating / 5.0) * 0.3
            # Fairness: inverse of total rides (simplified - use rides today in production)
            fairness_score = (1 / (driver.total_rides + 1)) * 0.2
            return distance_score + rating_score + fairness_score

        best = max(candidates, key=lambda row: score(row[0], row[1]))
        return best[0]

    # ── Ride Dispatch ──────────────────────────────────────────────────────────

    async def dispatch_ride(
        self,
        ride_id: UUID,
        db: AsyncSession,
    ) -> bool:
        """
        Find the best available driver and alert them.
        Called after the passenger confirms the fare quote.

        Returns True if a driver was found and alerted, False otherwise.
        """
        ride = await db.get(Ride, ride_id)
        if not ride:
            raise DispatchError(f"Ride {ride_id} not found")

        if ride.status != RideStatus.REQUESTED:
            raise DispatchError(f"Ride {ride_id} is not in REQUESTED state")

        # Extract pickup coordinates from the geometry column.
        # The value can arrive in three forms depending on how SQLAlchemy/
        # asyncpg serialised it on the round-trip:
        #   1. WKBElement  - geoalchemy2 native object
        #   2. EWKB hex    - raw hex string with embedded SRID (e.g. "0101...")
        #   3. EWKT string - "SRID=4326;POINT(lon lat)"
        from geoalchemy2.shape import to_shape
        from geoalchemy2.elements import WKBElement, WKTElement
        from shapely import wkt as shapely_wkt
        loc = ride.pickup_location
        if isinstance(loc, (WKBElement, WKTElement)):
            pickup_shape = to_shape(loc)
        elif isinstance(loc, str) and loc.upper().startswith("SRID="):
            # Strip the SRID prefix and parse as WKT
            wkt_part = loc.split(";", 1)[1]
            pickup_shape = shapely_wkt.loads(wkt_part)
        else:
            # Try raw EWKB hex as last resort
            pickup_shape = to_shape(WKBElement(loc))
        pickup_lon, pickup_lat = pickup_shape.x, pickup_shape.y

        # Get list of already-tried drivers for this ride (for reassignment)
        exclude_ids = await self._get_tried_driver_ids(ride_id)

        driver = await self.find_best_driver(
            pickup_lat=pickup_lat,
            pickup_lon=pickup_lon,
            db=db,
            exclude_driver_ids=exclude_ids,
        )

        if not driver:
            logger.warning("dispatch_no_driver_found", ride_id=str(ride_id))
            return False

        # Update ride status to MATCHED
        await db.execute(
            update(Ride)
            .where(Ride.id == ride_id)
            .values(
                status=RideStatus.MATCHED,
                driver_id=driver.user_id,
                matched_at=datetime.now(timezone.utc),
            )
        )

        # Mark driver as ON_RIDE to remove from available pool immediately
        await db.execute(
            update(DriverProfile)
            .where(DriverProfile.id == driver.id)
            .values(availability=DriverAvailability.ON_RIDE)
        )

        await db.commit()

        # Track acceptance window in Redis
        await ride_acceptance_tracker.set_pending(
            ride_id=str(ride_id),
            driver_id=str(driver.user_id),
        )

        # Track that we tried this driver (for reassignment)
        await self._record_tried_driver(ride_id, driver.user_id)

        # Send FCM push notification to driver
        from app.services.notifications import notification_service
        await notification_service.notify_driver_new_ride(
            driver_user_id=driver.user_id,
            ride=ride,
            db=db,
        )

        # Schedule timeout check via Celery (best-effort - no worker on free tier)
        try:
            from app.tasks.dispatch_tasks import check_driver_acceptance_timeout
            check_driver_acceptance_timeout.apply_async(
                args=[str(ride_id)],
                countdown=settings.DISPATCH_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS + 5,
            )
        except Exception as _celery_err:
            logger.warning(
                "celery_task_skipped",
                error=str(_celery_err),
                ride_id=str(ride_id),
            )

        logger.info(
            "ride_dispatched",
            ride_id=str(ride_id),
            driver_id=str(driver.user_id),
        )

        return True

    async def handle_driver_acceptance(
        self,
        ride_id: UUID,
        driver_user_id: UUID,
        db: AsyncSession,
    ) -> bool:
        """
        Driver tapped Accept on the ride alert.
        Validates the acceptance window is still open and updates ride state.
        """
        pending_driver = await ride_acceptance_tracker.get_pending_driver(str(ride_id))

        if not pending_driver:
            logger.warning("acceptance_window_expired", ride_id=str(ride_id))
            return False

        if pending_driver != str(driver_user_id):
            logger.warning("acceptance_wrong_driver", ride_id=str(ride_id))
            return False

        await db.execute(
            update(Ride)
            .where(Ride.id == ride_id)
            .values(
                status=RideStatus.ACCEPTED,
                accepted_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

        await ride_acceptance_tracker.clear(str(ride_id))

        # Notify passenger their driver is on the way
        from app.services.notifications import notification_service
        await notification_service.notify_passenger_driver_accepted(ride_id=ride_id, db=db)

        logger.info("driver_accepted_ride", ride_id=str(ride_id), driver_id=str(driver_user_id))
        return True

    async def handle_driver_decline(
        self,
        ride_id: UUID,
        driver_user_id: UUID,
        db: AsyncSession,
    ) -> None:
        """
        Driver declined the ride or timed out.
        Free the driver and attempt reassignment.
        """
        # Free the driver back to ONLINE
        driver = await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == driver_user_id)
        )
        driver_profile = driver.scalar_one_or_none()

        if driver_profile:
            await db.execute(
                update(DriverProfile)
                .where(DriverProfile.id == driver_profile.id)
                .values(availability=DriverAvailability.ONLINE)
            )

        await ride_acceptance_tracker.clear(str(ride_id))

        # Check if we have reassignment attempts left
        ride = await db.get(Ride, ride_id)
        if not ride:
            return

        if ride.reassignment_count >= settings.DISPATCH_MAX_REASSIGNMENT_ATTEMPTS:
            # Cancel the ride - no drivers available
            await db.execute(
                update(Ride)
                .where(Ride.id == ride_id)
                .values(
                    status=RideStatus.CANCELLED,
                    cancelled_at=datetime.now(timezone.utc),
                    cancellation_reason="No drivers available after maximum reassignment attempts",
                )
            )
            await db.commit()

            # Notify passenger
            from app.services.notifications import notification_service
            await notification_service.notify_passenger_no_driver(ride_id=ride_id, db=db)
            return

        # Increment reassignment count and try again
        await db.execute(
            update(Ride)
            .where(Ride.id == ride_id)
            .values(
                status=RideStatus.REQUESTED,
                driver_id=None,
                reassignment_count=ride.reassignment_count + 1,
            )
        )
        await db.commit()

        logger.info("ride_reassigning", ride_id=str(ride_id), attempt=ride.reassignment_count + 1)
        await self.dispatch_ride(ride_id=ride_id, db=db)

    # ── GPS State Machine ──────────────────────────────────────────────────────

    async def process_driver_location_update(
        self,
        driver_user_id: UUID,
        latitude: float,
        longitude: float,
        speed_kmh: float,
        accuracy_meters: float,
        recorded_at: datetime,
        db: AsyncSession,
    ) -> None:
        """
        Called every time the driver app sends a GPS update.
        This is the heartbeat of the entire ride tracking system.

        Responsibilities:
        1. Update driver's current location in Redis (real-time)
        2. Update driver's location in PostgreSQL (persistent)
        3. If driver has an active ride - record GPS trail point
        4. Check geofence conditions and trigger state transitions
        """

        # 1. Update Redis (fast - for dispatch queries)
        await driver_location_store.update(
            driver_id=str(driver_user_id),
            latitude=latitude,
            longitude=longitude,
        )

        # 2. Update PostgreSQL (persistent - for history and matching)
        wkt_point = f"SRID=4326;POINT({longitude} {latitude})"
        await db.execute(
            update(DriverProfile)
            .where(DriverProfile.user_id == driver_user_id)
            .values(
                current_location=wkt_point,
                location_updated_at=datetime.now(timezone.utc),
            )
        )

        # 3. Check for active ride
        active_ride = await self._get_active_ride_for_driver(driver_user_id, db)

        if not active_ride:
            await db.commit()
            return

        # 4. Record GPS trail point for this ride
        trail_point_wkt = f"SRID=4326;POINT({longitude} {latitude})"
        trail_point = GPSTrailPoint(
            ride_id=active_ride.id,
            driver_id=driver_user_id,
            location=trail_point_wkt,
            speed_kmh=speed_kmh,
            accuracy_meters=accuracy_meters,
            recorded_at=recorded_at,
        )
        db.add(trail_point)

        # 5. GPS-based auto state transitions
        await self._check_geofence_transitions(
            ride=active_ride,
            driver_lat=latitude,
            driver_lon=longitude,
            speed_kmh=speed_kmh,
            db=db,
        )

        await db.commit()

    async def _check_geofence_transitions(
        self,
        ride: Ride,
        driver_lat: float,
        driver_lon: float,
        speed_kmh: float,
        db: AsyncSession,
    ) -> None:
        """
        GPS is the source of truth.
        Auto-trigger state transitions based on real driver position.
        """
        from geoalchemy2.shape import to_shape
        from math import radians, sin, cos, sqrt, atan2

        def haversine_meters(lat1, lon1, lat2, lon2) -> float:
            R = 6371000
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
            return R * 2 * atan2(sqrt(a), sqrt(1 - a))

        # Extract pickup and dropoff coordinates
        pickup_shape = to_shape(ride.pickup_location)
        dropoff_shape = to_shape(ride.dropoff_location)

        pickup_lat, pickup_lon = pickup_shape.y, pickup_shape.x
        dropoff_lat, dropoff_lon = dropoff_shape.y, dropoff_shape.x

        dist_to_pickup = haversine_meters(driver_lat, driver_lon, pickup_lat, pickup_lon)
        dist_to_dropoff = haversine_meters(driver_lat, driver_lon, dropoff_lat, dropoff_lon)

        now = datetime.now(timezone.utc)

        # ACCEPTED -> DRIVER_ARRIVING: driver is within arrival geofence
        if (
            ride.status == RideStatus.ACCEPTED
            and dist_to_pickup <= settings.DISPATCH_ARRIVAL_GEOFENCE_RADIUS_METERS
        ):
            await db.execute(
                update(Ride)
                .where(Ride.id == ride.id)
                .values(status=RideStatus.DRIVER_ARRIVING, driver_arrived_at=now)
            )
            from app.services.notifications import notification_service
            await notification_service.notify_passenger_driver_arrived(ride_id=ride.id, db=db)
            logger.info("auto_transition_driver_arriving", ride_id=str(ride.id))

        # DRIVER_ARRIVING -> IN_PROGRESS: driver moving away from pickup
        # Only auto-start if driver has been at pickup for auto-start delay
        elif (
            ride.status == RideStatus.DRIVER_ARRIVING
            and ride.driver_arrived_at
            and speed_kmh > 3  # driver is moving
            and dist_to_pickup > settings.DISPATCH_ARRIVAL_GEOFENCE_RADIUS_METERS * 2
        ):
            seconds_at_pickup = (now - ride.driver_arrived_at).total_seconds()
            if seconds_at_pickup >= settings.DISPATCH_AUTO_START_DELAY_SECONDS:
                await db.execute(
                    update(Ride)
                    .where(Ride.id == ride.id)
                    .values(status=RideStatus.IN_PROGRESS, started_at=now)
                )
                logger.info("auto_transition_in_progress", ride_id=str(ride.id))

        # IN_PROGRESS -> COMPLETED: driver stationary near destination
        elif (
            ride.status == RideStatus.IN_PROGRESS
            and dist_to_dropoff <= settings.DISPATCH_COMPLETION_GEOFENCE_RADIUS_METERS
            and speed_kmh < 2   # effectively stationary
        ):
            # Schedule auto-complete after stationary delay
            from app.tasks.dispatch_tasks import auto_complete_ride
            auto_complete_ride.apply_async(
                args=[str(ride.id)],
                countdown=settings.DISPATCH_AUTO_COMPLETE_STATIONARY_SECONDS,
            )
            logger.info("auto_complete_scheduled", ride_id=str(ride.id))

    async def complete_ride(
        self,
        ride_id: UUID,
        db: AsyncSession,
    ) -> None:
        """
        Finalize a ride. Calculate actual fare from GPS trail.
        Trigger payment split.
        """
        ride = await db.get(Ride, ride_id)
        if not ride or ride.status not in [RideStatus.IN_PROGRESS, RideStatus.DRIVER_ARRIVING]:
            return

        now = datetime.now(timezone.utc)
        duration_minutes = 0.0

        if ride.started_at:
            duration_minutes = (now - ride.started_at).total_seconds() / 60

        # Calculate actual fare from GPS trail
        trail_result = await db.execute(
            select(GPSTrailPoint)
            .where(GPSTrailPoint.ride_id == ride_id)
            .order_by(GPSTrailPoint.recorded_at)
        )
        trail_points_raw = trail_result.scalars().all()

        if trail_points_raw:
            from geoalchemy2.shape import to_shape
            trail_coords = []
            for point in trail_points_raw:
                shape = to_shape(point.location)
                trail_coords.append((shape.y, shape.x))  # (lat, lon)

            fare_breakdown = routing_service.calculate_actual_fare_from_trail(
                trail_points=trail_coords,
                duration_minutes=duration_minutes,
            )
            final_fare = fare_breakdown.total_ugx
            actual_distance = fare_breakdown.distance_km
        else:
            # Fallback to estimated fare if no trail data
            final_fare = ride.estimated_fare_ugx
            actual_distance = ride.estimated_distance_km

        commission, driver_earnings = routing_service.calculate_commission(final_fare)

        # Update ride record
        await db.execute(
            update(Ride)
            .where(Ride.id == ride_id)
            .values(
                status=RideStatus.COMPLETED,
                completed_at=now,
                final_fare_ugx=final_fare,
                commission_ugx=commission,
                driver_earnings_ugx=driver_earnings,
                actual_distance_km=actual_distance,
                actual_duration_minutes=duration_minutes,
            )
        )

        # Free the driver back to ONLINE
        await db.execute(
            update(DriverProfile)
            .where(DriverProfile.user_id == ride.driver_id)
            .values(
                availability=DriverAvailability.ONLINE,
                total_rides=DriverProfile.total_rides + 1,
                total_earnings_ugx=DriverProfile.total_earnings_ugx + driver_earnings,
            )
        )

        await db.commit()

        # Trigger payment
        from app.services.payment import payment_service
        await payment_service.process_ride_payment(ride_id=ride_id, db=db)

        logger.info(
            "ride_completed",
            ride_id=str(ride_id),
            final_fare=final_fare,
            commission=commission,
            driver_earnings=driver_earnings,
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _get_active_ride_for_driver(
        self,
        driver_user_id: UUID,
        db: AsyncSession,
    ) -> Optional[Ride]:
        result = await db.execute(
            select(Ride).where(
                and_(
                    Ride.driver_id == driver_user_id,
                    Ride.status.in_([
                        RideStatus.ACCEPTED,
                        RideStatus.DRIVER_ARRIVING,
                        RideStatus.IN_PROGRESS,
                    ])
                )
            )
        )
        return result.scalar_one_or_none()

    async def _get_tried_driver_ids(self, ride_id: UUID) -> list[UUID]:
        """Get list of driver IDs already tried for this ride (from Redis)."""
        from app.services.cache import get_redis
        redis = await get_redis()
        raw = await redis.get(f"ride_tried_drivers:{ride_id}")
        if not raw:
            return []
        return [UUID(d) for d in json.loads(raw)]

    async def _record_tried_driver(self, ride_id: UUID, driver_id: UUID) -> None:
        from app.services.cache import get_redis
        redis = await get_redis()
        key = f"ride_tried_drivers:{ride_id}"
        existing = await self._get_tried_driver_ids(ride_id)
        existing.append(driver_id)
        await redis.setex(key, 600, json.dumps([str(d) for d in existing]))


# ── Singleton ──────────────────────────────────────────────────────────────────
dispatch_service = DispatchService()
