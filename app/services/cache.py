"""
app/services/cache.py
----------------------
Redis client and all caching utilities.
Driver real-time locations are stored here (updated every 3 seconds).
Much faster than hitting PostgreSQL for location data.
"""

import json
from typing import Optional
from functools import lru_cache

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return the global Redis connection. Created once on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis():
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


# ── Driver Location Store ──────────────────────────────────────────────────────
# Keys: driver_location:{driver_id}
# Value: JSON {"lat": float, "lon": float, "updated_at": str}
# TTL: REDIS_DRIVER_LOCATION_TTL seconds (default 30)
# If a driver's key has expired, they are considered offline/stale.

class DriverLocationStore:
    """
    Fast in-memory store for driver GPS locations.
    The mobile app pushes a location update every 3 seconds.
    The dispatch system reads from here, not PostgreSQL.
    """

    KEY_PREFIX = "driver_location"

    async def update(
        self,
        driver_id: str,
        latitude: float,
        longitude: float,
    ) -> None:
        redis = await get_redis()
        key = f"{self.KEY_PREFIX}:{driver_id}"
        value = json.dumps({
            "lat": latitude,
            "lon": longitude,
        })
        await redis.setex(key, settings.REDIS_DRIVER_LOCATION_TTL, value)

    async def get(self, driver_id: str) -> Optional[dict]:
        """Returns None if driver location is stale or driver is offline."""
        redis = await get_redis()
        key = f"{self.KEY_PREFIX}:{driver_id}"
        raw = await redis.get(key)
        if not raw:
            return None
        return json.loads(raw)

    async def delete(self, driver_id: str) -> None:
        redis = await get_redis()
        await redis.delete(f"{self.KEY_PREFIX}:{driver_id}")


# ── OTP Store ─────────────────────────────────────────────────────────────────
# Stores OTP attempt counts for rate limiting
# Key: otp_attempts:{phone_number}

class OTPRateLimiter:
    MAX_ATTEMPTS = 5
    WINDOW_SECONDS = 600   # 10 minutes

    async def increment(self, phone_number: str) -> int:
        redis = await get_redis()
        key = f"otp_attempts:{phone_number}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, self.WINDOW_SECONDS)
        return count

    async def is_blocked(self, phone_number: str) -> bool:
        redis = await get_redis()
        key = f"otp_attempts:{phone_number}"
        raw = await redis.get(key)
        return int(raw or 0) >= self.MAX_ATTEMPTS

    async def reset(self, phone_number: str) -> None:
        redis = await get_redis()
        await redis.delete(f"otp_attempts:{phone_number}")


# ── Ride Acceptance Timeout ────────────────────────────────────────────────────
# Tracks pending ride acceptance windows
# Key: ride_acceptance:{ride_id}

class RideAcceptanceTracker:

    async def set_pending(self, ride_id: str, driver_id: str) -> None:
        """Mark a ride as waiting for driver acceptance."""
        redis = await get_redis()
        key = f"ride_acceptance:{ride_id}"
        await redis.setex(
            key,
            settings.DISPATCH_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS,
            driver_id,
        )

    async def get_pending_driver(self, ride_id: str) -> Optional[str]:
        """Returns driver_id if the ride is still pending acceptance, else None."""
        redis = await get_redis()
        return await redis.get(f"ride_acceptance:{ride_id}")

    async def clear(self, ride_id: str) -> None:
        redis = await get_redis()
        await redis.delete(f"ride_acceptance:{ride_id}")


# ── Singletons ─────────────────────────────────────────────────────────────────
driver_location_store = DriverLocationStore()
otp_rate_limiter = OTPRateLimiter()
ride_acceptance_tracker = RideAcceptanceTracker()
