"""
app/services/geocoding.py
--------------------------
Geocoding and reverse geocoding using Nominatim (OpenStreetMap).
Converts location names to GPS coordinates and back.

Design decisions:
- Results are cached in Redis to reduce API calls and respect Nominatim's
  usage policy (max 1 request/second).
- All searches are biased toward the Kabale region bounding box.
- Falls back gracefully if Nominatim cannot resolve a location.
"""

import json
import hashlib
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cache import get_redis

logger = get_logger(__name__)

# Bounding box for Kabale District, Uganda (prevents ambiguous results)
# Format: min_lon, min_lat, max_lon, max_lat
KABALE_BBOX = "29.85,-1.40,30.10,-0.90"
KABALE_CENTER_LAT = -1.2492
KABALE_CENTER_LON = 29.9847

# Cache TTL for geocoded results (24 hours - places don't move)
GEOCODE_CACHE_TTL = 86400


class GeocodingError(Exception):
    pass


class Coordinates:
    """Simple value object for a GPS coordinate pair."""

    def __init__(self, latitude: float, longitude: float, display_name: str = ""):
        self.latitude = latitude
        self.longitude = longitude
        self.display_name = display_name

    def to_wkt(self) -> str:
        """Return as WKT Point string for PostGIS insertion."""
        return f"SRID=4326;POINT({self.longitude} {self.latitude})"

    def to_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "display_name": self.display_name,
        }

    def __repr__(self):
        return f"Coordinates(lat={self.latitude}, lon={self.longitude})"


class GeocodingService:
    """
    Wraps Nominatim API with caching and Kabale-region bias.
    Use as a singleton - instantiated once in app startup.
    """

    def __init__(self):
        self.base_url = settings.NOMINATIM_BASE_URL
        self.headers = {
            "User-Agent": settings.NOMINATIM_USER_AGENT,
            "Accept-Language": "en",
        }

    def _cache_key(self, query: str) -> str:
        normalized = query.lower().strip()
        return f"geocode:{hashlib.md5(normalized.encode()).hexdigest()}"

    async def geocode(self, location_name: str) -> Optional[Coordinates]:
        """
        Convert a location name to GPS coordinates.
        Returns None if the location cannot be resolved.

        Example:
            coords = await geocoding.geocode("Kabale University")
            # Coordinates(lat=-1.2492, lon=29.9847)
        """
        cache_key = self._cache_key(location_name)
        redis = await get_redis()

        # Try cache first
        cached = await redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            logger.debug("geocode_cache_hit", query=location_name)
            return Coordinates(
                latitude=data["latitude"],
                longitude=data["longitude"],
                display_name=data["display_name"],
            )

        # Query Nominatim
        params = {
            "q": f"{location_name}, Kabale, Uganda",
            "format": "json",
            "limit": 1,
            "viewbox": KABALE_BBOX,
            "bounded": 0,           # 0 = prefer but don't restrict to bbox
            "countrycodes": "ug",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers=self.headers,
                )
                response.raise_for_status()
                results = response.json()

            if not results:
                logger.warning("geocode_no_results", query=location_name)
                return None

            result = results[0]
            coords = Coordinates(
                latitude=float(result["lat"]),
                longitude=float(result["lon"]),
                display_name=result.get("display_name", location_name),
            )

            # Cache the result
            await redis.setex(
                cache_key,
                GEOCODE_CACHE_TTL,
                json.dumps(coords.to_dict()),
            )

            logger.info("geocode_success", query=location_name, coords=str(coords))
            return coords

        except httpx.HTTPError as e:
            logger.error("geocode_http_error", query=location_name, error=str(e))
            raise GeocodingError(f"Geocoding service unavailable: {str(e)}")

    async def reverse_geocode(self, latitude: float, longitude: float) -> Optional[str]:
        """
        Convert GPS coordinates to a human-readable location name.
        Used for generating meaningful ride records from raw coordinates.
        """
        cache_key = f"reverse_geocode:{latitude:.5f}:{longitude:.5f}"
        redis = await get_redis()

        cached = await redis.get(cache_key)
        if cached:
            return cached.decode()

        params = {
            "lat": latitude,
            "lon": longitude,
            "format": "json",
            "zoom": 17,    # street level
            "addressdetails": 1,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/reverse",
                    params=params,
                    headers=self.headers,
                )
                response.raise_for_status()
                result = response.json()

            display_name = result.get("display_name", f"{latitude}, {longitude}")

            await redis.setex(cache_key, GEOCODE_CACHE_TTL, display_name.encode())
            return display_name

        except httpx.HTTPError as e:
            logger.error("reverse_geocode_error", lat=latitude, lon=longitude, error=str(e))
            return f"{latitude:.4f}, {longitude:.4f}"


# ── Singleton instance ─────────────────────────────────────────────────────────
geocoding_service = GeocodingService()
