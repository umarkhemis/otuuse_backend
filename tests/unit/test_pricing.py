"""
tests/unit/test_pricing.py
---------------------------
Unit tests for fare calculation, commission split,
GPS haversine distance, and security utilities.
These tests have zero external dependencies - no DB, no APIs.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.services.routing import RoutingService
from app.core.security import (
    generate_otp, hash_otp, verify_otp,
    create_access_token, decode_token,
    hash_pin, verify_pin,
)


# ── Fare Calculation ───────────────────────────────────────────────────────────

class TestFareCalculation:

    def setup_method(self):
        self.service = RoutingService()

    def test_basic_fare_calculation(self):
        """Standard fare for a 3km, 10-minute ride."""
        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_BASE_FEE_UGX = 1000
            mock_settings.PRICING_RATE_PER_KM_UGX = 1200
            mock_settings.PRICING_RATE_PER_MINUTE_UGX = 50
            mock_settings.PRICING_MINIMUM_FARE_UGX = 2000

            breakdown = self.service.calculate_fare(distance_km=3.0, duration_minutes=10.0)

            # 1000 + (3 * 1200) + (10 * 50) = 1000 + 3600 + 500 = 5100 -> rounded to 5100
            assert breakdown.base_fee_ugx == 1000
            assert breakdown.distance_fee_ugx == 3600
            assert breakdown.time_fee_ugx == 500
            assert breakdown.total_ugx >= 2000  # minimum fare enforced

    def test_minimum_fare_enforced(self):
        """Very short rides should still hit the minimum fare."""
        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_BASE_FEE_UGX = 500
            mock_settings.PRICING_RATE_PER_KM_UGX = 500
            mock_settings.PRICING_RATE_PER_MINUTE_UGX = 20
            mock_settings.PRICING_MINIMUM_FARE_UGX = 2000

            breakdown = self.service.calculate_fare(distance_km=0.1, duration_minutes=1.0)
            assert breakdown.total_ugx == 2000

    def test_fare_rounds_to_nearest_100(self):
        """Fare should round to nearest 100 UGX for clean amounts."""
        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_BASE_FEE_UGX = 1000
            mock_settings.PRICING_RATE_PER_KM_UGX = 1000
            mock_settings.PRICING_RATE_PER_MINUTE_UGX = 33
            mock_settings.PRICING_MINIMUM_FARE_UGX = 2000

            breakdown = self.service.calculate_fare(distance_km=2.0, duration_minutes=5.0)
            assert breakdown.total_ugx % 100 == 0

    def test_commission_split_15_percent(self):
        """Commission should be 15% and driver gets 85%."""
        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_COMMISSION_PERCENT = 15.0

            commission, driver_earnings = self.service.calculate_commission(10000)

            assert commission == 1500
            assert driver_earnings == 8500
            assert commission + driver_earnings == 10000

    def test_commission_split_sums_to_fare(self):
        """Commission + driver earnings must always equal the fare."""
        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_COMMISSION_PERCENT = 15.0

            for fare in [2000, 5000, 7500, 10000, 25000]:
                commission, driver_earnings = self.service.calculate_commission(fare)
                assert commission + driver_earnings == fare

    def test_haversine_distance_kabale_points(self):
        """
        Test distance calculation between two known Kabale points.
        Kabale University to Kabale Market is roughly 1-2km.
        """
        trail = [
            (-1.2492, 29.9847),  # Kabale University area
            (-1.2510, 29.9860),  # slightly south-east
            (-1.2530, 29.9880),  # continuing
            (-1.2550, 29.9900),  # Kabale market area
        ]

        with patch("app.services.routing.settings") as mock_settings:
            mock_settings.PRICING_BASE_FEE_UGX = 1000
            mock_settings.PRICING_RATE_PER_KM_UGX = 1200
            mock_settings.PRICING_RATE_PER_MINUTE_UGX = 50
            mock_settings.PRICING_MINIMUM_FARE_UGX = 2000

            breakdown = self.service.calculate_actual_fare_from_trail(
                trail_points=trail,
                duration_minutes=5.0,
            )

        # Distance should be well under 5km for these close points
        assert breakdown.distance_km < 5.0
        assert breakdown.total_ugx >= 2000


# ── Security Utilities ─────────────────────────────────────────────────────────

class TestOTP:

    def test_otp_is_6_digits(self):
        otp = generate_otp()
        assert len(otp) == 6
        assert otp.isdigit()

    def test_otp_is_random(self):
        otps = {generate_otp() for _ in range(100)}
        # With 6 digits and 100 samples, we expect very high uniqueness
        assert len(otps) > 90

    def test_otp_hash_verify_correct(self):
        with patch("app.core.security.settings") as mock_settings:
            mock_settings.APP_SECRET_KEY = "test-secret-key-32-characters-long"
            otp = generate_otp()
            hashed = hash_otp(otp)
            assert verify_otp(otp, hashed) is True

    def test_otp_hash_verify_wrong(self):
        with patch("app.core.security.settings") as mock_settings:
            mock_settings.APP_SECRET_KEY = "test-secret-key-32-characters-long"
            otp = generate_otp()
            hashed = hash_otp(otp)
            assert verify_otp("000000", hashed) is False

    def test_otp_hash_is_deterministic(self):
        with patch("app.core.security.settings") as mock_settings:
            mock_settings.APP_SECRET_KEY = "test-secret-key-32-characters-long"
            otp = "123456"
            hash1 = hash_otp(otp)
            hash2 = hash_otp(otp)
            assert hash1 == hash2


class TestJWT:

    def test_access_token_round_trip(self):
        with patch("app.core.security.settings") as mock_settings:
            mock_settings.APP_SECRET_KEY = "test-secret-key-that-is-32-chars!!"
            mock_settings.JWT_ALGORITHM = "HS256"
            mock_settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = 60

            token = create_access_token(subject="user-123", role="passenger")
            payload = decode_token(token)

            assert payload["sub"] == "user-123"
            assert payload["role"] == "passenger"
            assert payload["type"] == "access"

    def test_token_has_expiry(self):
        with patch("app.core.security.settings") as mock_settings:
            mock_settings.APP_SECRET_KEY = "test-secret-key-that-is-32-chars!!"
            mock_settings.JWT_ALGORITHM = "HS256"
            mock_settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = 60

            token = create_access_token(subject="user-123", role="driver")
            payload = decode_token(token)
            assert "exp" in payload


class TestPIN:

    def test_pin_hash_verify_correct(self):
        pin = "1234"
        hashed = hash_pin(pin)
        assert verify_pin(pin, hashed) is True

    def test_pin_hash_verify_wrong(self):
        hashed = hash_pin("1234")
        assert verify_pin("9999", hashed) is False

    def test_pin_hash_is_not_plain_text(self):
        pin = "1234"
        hashed = hash_pin(pin)
        assert pin not in hashed
        assert len(hashed) > 20
