"""
app/core/security.py
--------------------
All authentication and cryptographic utilities.
- JWT access and refresh token generation and verification
- OTP generation and hashing
- PIN hashing for driver logins
"""

import secrets
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings


# ── Password / PIN hashing context ────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── JWT ────────────────────────────────────────────────────────────────────────

def create_access_token(subject: str, role: str) -> str:
    """
    Create a short-lived JWT access token.
    subject: typically the user's UUID as a string.
    role: 'passenger' | 'driver' | 'admin'
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.APP_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str, role: str) -> str:
    """
    Create a long-lived JWT refresh token.
    Stored in the database and invalidated on logout or suspicious activity.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
        "jti": secrets.token_hex(16),   # unique token ID for revocation
    }
    return jwt.encode(payload, settings.APP_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT token.
    Raises JWTError if the token is invalid, expired, or tampered with.
    """
    return jwt.decode(
        token,
        settings.APP_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


def verify_token_type(payload: dict, expected_type: str) -> bool:
    return payload.get("type") == expected_type


# ── PIN hashing (for driver accounts) ─────────────────────────────────────────

def hash_pin(pin: str) -> str:
    """Hash a 4-6 digit PIN using bcrypt."""
    return pwd_context.hash(pin)


def verify_pin(plain_pin: str, hashed_pin: str) -> bool:
    return pwd_context.verify(plain_pin, hashed_pin)


# ── OTP ────────────────────────────────────────────────────────────────────────

def generate_otp(length: int = 6) -> str:
    """Generate a cryptographically secure numeric OTP."""
    return "".join([str(secrets.randbelow(10)) for _ in range(length)])


def hash_otp(otp: str) -> str:
    """
    Hash an OTP before storing it in the database.
    We never store OTPs in plain text.
    Uses SHA-256 with the app secret as a key (HMAC) to prevent rainbow table attacks.
    """
    return hmac.new(
        settings.APP_SECRET_KEY.encode(),
        otp.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_otp(plain_otp: str, hashed_otp: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    expected = hash_otp(plain_otp)
    return hmac.compare_digest(expected, hashed_otp)


# ── Webhook signature verification (PesaPal IPN) ──────────────────────────────

def verify_pesapal_signature(payload: str, signature: str, secret: str) -> bool:
    """
    Verify that a PesaPal IPN callback is genuinely from PesaPal.
    Uses HMAC-SHA256 to compare the received signature against a recomputed one.
    """
    expected = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
