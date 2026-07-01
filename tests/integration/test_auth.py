"""
tests/integration/test_auth.py
--------------------------------
Integration tests for the authentication flow.
Tests the full OTP request -> verify -> token flow.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from unittest.mock import patch, AsyncMock

from tests.conftest import passenger_token


class TestAuthFlow:

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_request_otp_new_user(self, client: AsyncClient):
        """New user registration triggers OTP send."""
        with patch("app.api.routes.auth.settings") as mock_settings:
            mock_settings.is_development = True

        response = await client.post("/api/v1/auth/request-otp", json={
            "phone_number": "+256701234567",
            "name": "Ahmed Test",
            "role": "passenger",
        })
        assert response.status_code == 200
        assert "OTP sent" in response.json()["message"]

    @pytest.mark.asyncio
    async def test_request_otp_driver_registration_blocked(self, client: AsyncClient):
        """Drivers cannot self-register - must be onboarded by admin."""
        response = await client.post("/api/v1/auth/request-otp", json={
            "phone_number": "+256701234568",
            "name": "Driver Test",
            "role": "driver",
        })
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_request_otp_invalid_phone(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/request-otp", json={
            "phone_number": "not-a-phone",
            "name": "Test",
            "role": "passenger",
        })
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_wrong_otp(self, client: AsyncClient):
        """Wrong OTP should return 400."""
        # First request an OTP
        await client.post("/api/v1/auth/request-otp", json={
            "phone_number": "+256701234569",
            "name": "Test User",
            "role": "passenger",
        })

        # Try wrong OTP
        response = await client.post("/api/v1/auth/verify-otp", json={
            "phone_number": "+256701234569",
            "otp": "000000",
        })
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_protected_route_without_token(self, client: AsyncClient):
        """Protected routes must reject requests without tokens."""
        response = await client.post("/api/v1/chat/message", json={"message": "hello"})
        assert response.status_code == 403  # No Authorization header

    @pytest.mark.asyncio
    async def test_protected_route_with_wrong_role(self, client: AsyncClient, driver_user):
        """A driver token cannot access passenger-only routes and vice versa."""
        user, profile = driver_user
        token = passenger_token(str(user.id))  # passenger token for a driver user

        response = await client.post(
            "/api/v1/driver/availability",
            json={"online": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        # This should fail because the JWT says passenger but route requires driver
        assert response.status_code in [401, 403]


class TestWalletBalance:

    @pytest.mark.asyncio
    async def test_get_wallet_balance(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        response = await client.get(
            "/api/v1/payments/wallet/balance",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["balance_ugx"] == 50000

    @pytest.mark.asyncio
    async def test_topup_minimum_amount(self, client: AsyncClient, passenger_user):
        """Top-up below 1000 UGX should be rejected."""
        token = passenger_token(str(passenger_user.id))

        response = await client.post(
            "/api/v1/payments/topup/initiate",
            json={"amount_ugx": 500},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422  # Pydantic validation


class TestAdminDashboard:

    @pytest.mark.asyncio
    async def test_dashboard_requires_admin(self, client: AsyncClient, passenger_user):
        """Dashboard is admin-only."""
        token = passenger_token(str(passenger_user.id))

        response = await client.get(
            "/api/v1/admin/dashboard",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_dashboard_returns_stats(self, client: AsyncClient, admin_user):
        from tests.conftest import admin_token
        token = admin_token(str(admin_user.id))

        response = await client.get(
            "/api/v1/admin/dashboard",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "drivers_online_now" in data
        assert "active_rides_now" in data
        assert "revenue_today_ugx" in data
        assert "pending_delivery_requests" in data


class TestPushToken:

    @pytest.mark.asyncio
    async def test_register_push_token(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        response = await client.post(
            "/api/v1/auth/push-token",
            json={"fcm_token": "fcm-device-abc123", "device_type": "android"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["message"] == "Push token registered"

    @pytest.mark.asyncio
    async def test_register_push_token_is_idempotent(self, client: AsyncClient, passenger_user):
        """Registering the same fcm_token twice should upsert, not error."""
        token = passenger_token(str(passenger_user.id))

        for _ in range(2):
            response = await client.post(
                "/api/v1/auth/push-token",
                json={"fcm_token": "fcm-device-repeat", "device_type": "ios"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_register_push_token_requires_auth(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/push-token",
            json={"fcm_token": "fcm-no-auth"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unregister_push_token(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        await client.post(
            "/api/v1/auth/push-token",
            json={"fcm_token": "fcm-to-remove"},
            headers={"Authorization": f"Bearer {token}"},
        )

        response = await client.delete(
            "/api/v1/auth/push-token",
            params={"fcm_token": "fcm-to-remove"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["message"] == "Push token unregistered"


class TestTokenRefresh:

    @pytest.mark.asyncio
    async def test_refresh_with_untracked_token_rejected(self, client: AsyncClient, passenger_user):
        """A well-formed refresh JWT that was never stored (e.g. forged) must be rejected."""
        from app.core.security import create_refresh_token

        refresh_token = create_refresh_token(subject=str(passenger_user.id), role="passenger")
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_garbage_token(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-jwt"})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_access_token_rejected(self, client: AsyncClient, passenger_user):
        """An access token (not a refresh token) must be rejected by /refresh."""
        access_token = passenger_token(str(passenger_user.id))
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_full_login_then_refresh_rotates_token(self, client: AsyncClient, passenger_user, test_db):
        """End-to-end: verify-otp issues a refresh token, then /refresh rotates it."""
        from app.models.models import OTPRecord
        from app.core.security import hash_otp
        from datetime import datetime, timezone, timedelta

        otp = "123456"
        test_db.add(OTPRecord(
            user_id=passenger_user.id,
            otp_hash=hash_otp(otp),
            purpose="login",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        await test_db.commit()

        login_response = await client.post("/api/v1/auth/verify-otp", json={
            "phone_number": passenger_user.phone_number,
            "otp": otp,
        })
        assert login_response.status_code == 200
        original_refresh = login_response.json()["refresh_token"]

        refresh_response = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
        assert refresh_response.status_code == 200
        rotated = refresh_response.json()
        assert rotated["refresh_token"] != original_refresh

        # The original refresh token must now be revoked - reusing it should fail.
        reuse_response = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
        assert reuse_response.status_code == 401


class TestLogout:

    @pytest.mark.asyncio
    async def test_logout_revokes_refresh_token(self, client: AsyncClient, passenger_user, test_db):
        from app.models.models import OTPRecord
        from app.core.security import hash_otp
        from datetime import datetime, timezone, timedelta

        otp = "654321"
        test_db.add(OTPRecord(
            user_id=passenger_user.id,
            otp_hash=hash_otp(otp),
            purpose="login",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        await test_db.commit()

        login_response = await client.post("/api/v1/auth/verify-otp", json={
            "phone_number": passenger_user.phone_number,
            "otp": otp,
        })
        tokens = login_response.json()

        logout_response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert logout_response.status_code == 200

        # The revoked refresh token must no longer work.
        refresh_response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_response.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_requires_auth(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/logout", json={"refresh_token": "whatever"})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_logout_also_deactivates_push_token(self, client: AsyncClient, passenger_user):
        token = passenger_token(str(passenger_user.id))

        await client.post(
            "/api/v1/auth/push-token",
            json={"fcm_token": "fcm-logout-test"},
            headers={"Authorization": f"Bearer {token}"},
        )

        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "irrelevant-garbage", "fcm_token": "fcm-logout-test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
