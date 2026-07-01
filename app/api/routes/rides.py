"""
app/api/routes/rides.py
-------------------------
Ride endpoints shared by passengers and drivers - the read and rate
operations that belong to whichever party is actually on the ride.

Driver-initiated ride actions (accept/decline/arrived/start/complete) live
in the driver routes; admin oversight (full ride list, GPS dispute trail)
lives in admin routes. This module covers:

- GET  /rides/active        - the caller's current in-progress ride, if any
- GET  /rides/{ride_id}     - a single ride's detail + live driver location
- POST /rides/{ride_id}/rate - post-ride rating, passenger -> driver or driver -> passenger
"""

import uuid as uuid_lib
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.db.session import get_db
from app.models.models import User, UserRole, Ride, RideStatus, DriverProfile
from app.services import crud
from app.services.cache import driver_location_store
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/rides", tags=["Rides"])


def _ensure_party_to_ride(ride: Ride, user: User) -> None:
    if user.id not in (ride.passenger_id, ride.driver_id):
        raise HTTPException(status_code=403, detail="You are not a party to this ride")


async def _serialize_ride(ride: Ride, db: AsyncSession) -> dict:
    """
    Builds the ride payload returned to passenger or driver.
    Driver identity stays minimal by design (no phone number) - the same
    privacy rule the chat agent's system prompt already enforces.
    """
    driver_info = None
    if ride.driver_id:
        result = await db.execute(
            select(User, DriverProfile)
            .join(DriverProfile, DriverProfile.user_id == User.id)
            .where(User.id == ride.driver_id)
        )
        row = result.first()
        if row:
            driver_user, driver_profile = row
            live = await driver_location_store.get(str(ride.driver_id))
            driver_info = {
                "name": driver_user.name,
                "rating": driver_profile.rating,
                "live_location": live,  # {"lat": .., "lon": ..} or None if stale/offline
                "location_updated_at": (
                    driver_profile.location_updated_at.isoformat()
                    if driver_profile.location_updated_at else None
                ),
            }

    return {
        "id": str(ride.id),
        "status": ride.status.value,
        "pickup_name": ride.pickup_name,
        "dropoff_name": ride.dropoff_name,
        "estimated_fare_ugx": ride.estimated_fare_ugx,
        "final_fare_ugx": ride.final_fare_ugx,
        "estimated_distance_km": ride.estimated_distance_km,
        "estimated_duration_minutes": ride.estimated_duration_minutes,
        "requested_at": ride.requested_at.isoformat(),
        "matched_at": ride.matched_at.isoformat() if ride.matched_at else None,
        "accepted_at": ride.accepted_at.isoformat() if ride.accepted_at else None,
        "driver_arrived_at": ride.driver_arrived_at.isoformat() if ride.driver_arrived_at else None,
        "started_at": ride.started_at.isoformat() if ride.started_at else None,
        "completed_at": ride.completed_at.isoformat() if ride.completed_at else None,
        "driver": driver_info,
        "passenger_rating": ride.passenger_rating,
        "driver_rating": ride.driver_rating,
    }


@router.get("/active")
async def get_active_ride(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the caller's current in-progress ride, or null.
    Lets the app recover state on launch without remembering a ride ID
    (e.g. after the app was killed mid-ride).
    """
    if current_user.role == UserRole.PASSENGER:
        ride = await crud.get_active_ride_for_passenger(user_id=current_user.id, db=db)
    elif current_user.role == UserRole.DRIVER:
        ride = await crud.get_active_ride_for_driver(user_id=current_user.id, db=db)
    else:
        raise HTTPException(status_code=403, detail="Admins do not have rides")

    if not ride:
        return {"active_ride": None}

    return {"active_ride": await _serialize_ride(ride, db)}


@router.get("/{ride_id}")
async def get_ride(
    ride_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single ride. Only the passenger or driver on the ride may view it."""
    try:
        ride = await db.get(Ride, uuid_lib.UUID(ride_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ride id")

    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")

    _ensure_party_to_ride(ride, current_user)

    return await _serialize_ride(ride, db)


class RateRideBody(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    review: Optional[str] = Field(default=None, max_length=500)


@router.post("/{ride_id}/rate")
async def rate_ride(
    ride_id: str,
    body: RateRideBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a rating after a completed ride.
    Passengers rate the driver (with an optional review).
    Drivers rate the passenger (rating only - no review field in that direction).
    Each party may rate a given ride exactly once.
    """
    try:
        ride = await db.get(Ride, uuid_lib.UUID(ride_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ride id")

    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")

    if ride.status not in (RideStatus.COMPLETED, RideStatus.PAID):
        raise HTTPException(status_code=400, detail="Ride must be completed before it can be rated")

    if current_user.role == UserRole.PASSENGER:
        if ride.passenger_id != current_user.id:
            raise HTTPException(status_code=403, detail="You are not the passenger on this ride")
        if ride.passenger_rating is not None:
            raise HTTPException(status_code=400, detail="You have already rated this ride")

        await db.execute(
            update(Ride)
            .where(Ride.id == ride.id)
            .values(passenger_rating=body.rating, passenger_review=body.review)
        )
        await db.commit()

        if ride.driver_id:
            from app.tasks.dispatch_tasks import recalculate_driver_rating
            recalculate_driver_rating.apply_async(args=[str(ride.driver_id)])

        logger.info("ride_rated", ride_id=ride_id, rated_by="passenger", rating=body.rating)
        return {"message": "Rating submitted"}

    if current_user.role == UserRole.DRIVER:
        if ride.driver_id != current_user.id:
            raise HTTPException(status_code=403, detail="You are not the driver on this ride")
        if ride.driver_rating is not None:
            raise HTTPException(status_code=400, detail="You have already rated this ride")

        await db.execute(
            update(Ride)
            .where(Ride.id == ride.id)
            .values(driver_rating=body.rating)
        )
        await db.commit()

        logger.info("ride_rated", ride_id=ride_id, rated_by="driver", rating=body.rating)
        return {"message": "Rating submitted"}

    raise HTTPException(status_code=403, detail="Only the passenger or driver on this ride may rate it")
