"""
app/services/routing.py
------------------------
Road distance and duration calculation using OpenRouteService (ORS).
ORS is free, open-source, and uses OpenStreetMap data - perfect for Kabale.

This service provides:
- Real road distance between two points (not straight-line)
- Estimated travel time accounting for road types
- Dynamic fare calculation based on the pricing formula
"""

import json
import hashlib
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cache import get_redis

logger = get_logger(__name__)

# Cache routing results for 1 hour (routes between the same points don't change much)
ROUTE_CACHE_TTL = 3600


class RoutingError(Exception):
    pass


@dataclass
class RouteResult:
    """Result from a routing query."""
    distance_km: float
    duration_minutes: float
    estimated_fare_ugx: int


@dataclass
class FareBreakdown:
    """Detailed fare calculation for transparency."""
    base_fee_ugx: int
    distance_fee_ugx: int
    time_fee_ugx: int
    total_ugx: int
    distance_km: float
    duration_minutes: float


class RoutingService:
    """
    Calculates road distances via OpenRouteService and computes fares.
    Configured to use motorcycle-optimized routing (driving-car profile
    is closest to motorcycle in ORS).
    """

    def __init__(self):
        self.base_url = settings.ORS_BASE_URL
        self.api_key = settings.ORS_API_KEY

    def _cache_key(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> str:
        key = f"{from_lat:.5f},{from_lon:.5f}->{to_lat:.5f},{to_lon:.5f}"
        return f"route:{hashlib.md5(key.encode()).hexdigest()}"

    async def get_route(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
    ) -> RouteResult:
        """
        Get road distance and estimated duration between two GPS coordinates.
        Returns a RouteResult with the fare pre-calculated.

        Raises RoutingError if ORS is unavailable or coordinates are invalid.
        """
        cache_key = self._cache_key(from_lat, from_lon, to_lat, to_lon)
        redis = await get_redis()

        # Try cache first
        cached = await redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            logger.debug("routing_cache_hit", from_lat=from_lat, to_lat=to_lat)
            return RouteResult(**data)

        # ORS expects [longitude, latitude] (GeoJSON order)
        payload = {
            "coordinates": [
                [from_lon, from_lat],
                [to_lon, to_lat],
            ],
        }

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.base_url}/directions/driving-car",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            # ORS returns distance in meters and duration in seconds
            summary = data["routes"][0]["summary"]
            distance_km = summary["distance"] / 1000
            duration_minutes = summary["duration"] / 60

            fare = self.calculate_fare(distance_km, duration_minutes)

            result = RouteResult(
                distance_km=round(distance_km, 2),
                duration_minutes=round(duration_minutes, 1),
                estimated_fare_ugx=fare.total_ugx,
            )

            await redis.setex(cache_key, ROUTE_CACHE_TTL, json.dumps({
                "distance_km": result.distance_km,
                "duration_minutes": result.duration_minutes,
                "estimated_fare_ugx": result.estimated_fare_ugx,
            }))

            logger.info(
                "routing_success",
                distance_km=result.distance_km,
                duration_min=result.duration_minutes,
                fare_ugx=result.estimated_fare_ugx,
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error("routing_http_error", status=e.response.status_code, error=str(e))
            raise RoutingError(f"Routing service error: {e.response.status_code}")
        except httpx.TimeoutException:
            # ORS timed out - fall back to Haversine straight-line estimate
            import math as _math
            R = 6371
            phi1 = _math.radians(from_lat)
            phi2 = _math.radians(to_lat)
            dphi = _math.radians(to_lat - from_lat)
            dlambda = _math.radians(to_lon - from_lon)
            a = (_math.sin(dphi/2)**2
                 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlambda/2)**2)
            straight_km = 2 * R * _math.asin(_math.sqrt(a))
            distance_km = round(straight_km * 1.35, 2)   # road-winding factor
            duration_minutes = round(distance_km / 20 * 60, 1)  # 20 km/h avg
            fare = self.calculate_fare(distance_km, duration_minutes)
            result = RouteResult(
                distance_km=distance_km,
                duration_minutes=duration_minutes,
                estimated_fare_ugx=fare.total_ugx,
            )
            logger.warning(
                "routing_timeout_haversine_fallback",
                distance_km=distance_km,
                fare_ugx=fare.total_ugx,
            )
            # Cache so subsequent requests for the same route don't hit ORS again
            await redis.setex(cache_key, ROUTE_CACHE_TTL, json.dumps({
                "distance_km": result.distance_km,
                "duration_minutes": result.duration_minutes,
                "estimated_fare_ugx": result.estimated_fare_ugx,
            }))
            return result
        except httpx.HTTPError as e:
            logger.error("routing_connection_error", error=str(e))
            raise RoutingError(f"Cannot reach routing service: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error("routing_parse_error", error=str(e))
            raise RoutingError("Unexpected response format from routing service")

    def calculate_fare(self, distance_km: float, duration_minutes: float) -> FareBreakdown:
        """
        Calculate fare using the platform's pricing formula.

        Formula:
            Fare = Base Fee + (Distance x Rate/km) + (Duration x Rate/min)

        All amounts in UGX (integer). Minimum fare enforced.
        """
        base_fee = settings.PRICING_BASE_FEE_UGX
        distance_fee = int(distance_km * settings.PRICING_RATE_PER_KM_UGX)
        time_fee = int(duration_minutes * settings.PRICING_RATE_PER_MINUTE_UGX)

        total = base_fee + distance_fee + time_fee
        total = max(total, settings.PRICING_MINIMUM_FARE_UGX)

        # Round to nearest 100 UGX for cleaner amounts
        total = round(total / 100) * 100

        return FareBreakdown(
            base_fee_ugx=base_fee,
            distance_fee_ugx=distance_fee,
            time_fee_ugx=time_fee,
            total_ugx=total,
            distance_km=distance_km,
            duration_minutes=duration_minutes,
        )

    def calculate_commission(self, fare_ugx: int) -> tuple[int, int]:
        """
        Split a fare into platform commission and driver earnings.
        Returns (commission_ugx, driver_earnings_ugx).
        """
        commission = int(fare_ugx * settings.PRICING_COMMISSION_PERCENT / 100)
        driver_earnings = fare_ugx - commission
        return commission, driver_earnings

    def calculate_actual_fare_from_trail(
        self,
        trail_points: list,
        duration_minutes: float,
    ) -> FareBreakdown:
        """
        Calculate the actual fare from recorded GPS trail points.
        Used on ride completion when actual distance may differ from estimated.

        trail_points: list of (lat, lon) tuples in order
        """
        from math import radians, sin, cos, sqrt, atan2

        def haversine(lat1, lon1, lat2, lon2) -> float:
            """Calculate distance between two GPS coordinates in km."""
            R = 6371  # Earth radius in km
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
            c = 2 * atan2(sqrt(a), sqrt(1 - a))
            return R * c

        total_distance = 0.0
        for i in range(1, len(trail_points)):
            lat1, lon1 = trail_points[i - 1]
            lat2, lon2 = trail_points[i]
            total_distance += haversine(lat1, lon1, lat2, lon2)

        return self.calculate_fare(total_distance, duration_minutes)


# ── Singleton instance ─────────────────────────────────────────────────────────
routing_service = RoutingService()
