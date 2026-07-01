"""
tests/integration/test_rides.py
---------------------------------
Integration tests for ride detail lookup, the active-ride app-resume
endpoint, and post-ride ratings.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.models.models import Ride, RideStatus
from tests.conftest import passenger_token, driver_token


def _make_ride(passenger_id, driver_id=None, status=RideStatus.REQUESTED, **overrides) -> Ride:
    defaults = dict(
        id=uuid.uuid4(),
        passenger_id=passenger_id,
        driver_id=driver_id,
        pickup_location="SRID=4326;POINT(29.9847 -1.2492)",
        dropoff_location="SRID=4326;POINT(29.9900 -1.2550)",
        pickup_name="Kabale University",
        dropoff_name="Kabale Market",
        estimated_distance_km=2.5,
        estimated_duration_minutes=12.0,
        estimated_fare_ugx=4000,
        status=status,
    )
    defaults.update(overrides)
    return Ride(**defaults)


class TestActiveRide:

    @pytest.mark.asyncio
    async def test_no_active_ride_returns_null(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        response = await client.get(
            "/api/v1/rides/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"active_ride": None}

    @pytest.mark.asyncio
    async def test_passenger_sees_their_active_ride(self, client: AsyncClient, passenger_user, test_db):
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, status=RideStatus.MATCHED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            "/api/v1/rides/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()["active_ride"]
        assert data is not None
        assert data["id"] == str(ride.id)
        assert data["status"] == "matched"

    @pytest.mark.asyncio
    async def test_completed_ride_is_not_active(self, client: AsyncClient, passenger_user, test_db):
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            "/api/v1/rides/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.json() == {"active_ride": None}

    @pytest.mark.asyncio
    async def test_driver_sees_their_active_ride(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = driver_token(str(driver.id))

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.IN_PROGRESS)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            "/api/v1/rides/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["active_ride"]["id"] == str(ride.id)

    @pytest.mark.asyncio
    async def test_driver_not_yet_matched_has_no_active_ride(self, client: AsyncClient, passenger_user, driver_user, test_db):
        """A REQUESTED ride has no driver attached yet, so it shouldn't show up for any driver."""
        driver, profile = driver_user
        token = driver_token(str(driver.id))

        ride = _make_ride(passenger_id=passenger_user.id, status=RideStatus.REQUESTED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            "/api/v1/rides/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.json() == {"active_ride": None}


class TestGetRide:

    @pytest.mark.asyncio
    async def test_passenger_can_view_own_ride(self, client: AsyncClient, passenger_user, test_db):
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            f"/api/v1/rides/{ride.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["pickup_name"] == "Kabale University"

    @pytest.mark.asyncio
    async def test_stranger_cannot_view_ride(self, client: AsyncClient, passenger_user, driver_user, test_db):
        """A driver who isn't on the ride must be rejected, even with a valid token."""
        driver, profile = driver_user
        token = driver_token(str(driver.id))

        ride = _make_ride(passenger_id=passenger_user.id, status=RideStatus.REQUESTED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.get(
            f"/api/v1/rides/{ride.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_nonexistent_ride_returns_404(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        response = await client.get(
            f"/api/v1/rides/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_ride_invalid_id_returns_400(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        response = await client.get(
            "/api/v1/rides/not-a-uuid",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_get_ride_requires_auth(self, client: AsyncClient):
        response = await client.get(f"/api/v1/rides/{uuid.uuid4()}")
        assert response.status_code == 403


class TestRateRide:

    @pytest.mark.asyncio
    async def test_passenger_rates_driver(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 5, "review": "Smooth ride, friendly driver."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

        await test_db.refresh(ride)
        assert ride.passenger_rating == 5
        assert ride.passenger_review == "Smooth ride, friendly driver."

    @pytest.mark.asyncio
    async def test_driver_rates_passenger_without_review(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = driver_token(str(driver.id))

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

        await test_db.refresh(ride)
        assert ride.driver_rating == 4

    @pytest.mark.asyncio
    async def test_cannot_rate_twice(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(
            passenger_id=passenger_user.id, driver_id=driver.id,
            status=RideStatus.COMPLETED, passenger_rating=5,
        )
        test_db.add(ride)
        await test_db.commit()

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cannot_rate_an_incomplete_ride(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.IN_PROGRESS)
        test_db.add(ride)
        await test_db.commit()

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_rating_out_of_range_rejected(self, client: AsyncClient, passenger_user, driver_user, test_db):
        driver, profile = driver_user
        token = passenger_token(str(passenger_user.id))

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 7},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422  # Pydantic validation

    @pytest.mark.asyncio
    async def test_stranger_cannot_rate_ride(self, client: AsyncClient, passenger_user, driver_user, test_db, admin_user):
        """A passenger who isn't on the ride must be rejected."""
        driver, profile = driver_user

        ride = _make_ride(passenger_id=passenger_user.id, driver_id=driver.id, status=RideStatus.COMPLETED)
        test_db.add(ride)
        await test_db.commit()

        from tests.conftest import admin_token
        token = admin_token(str(admin_user.id))

        response = await client.post(
            f"/api/v1/rides/{ride.id}/rate",
            json={"rating": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
