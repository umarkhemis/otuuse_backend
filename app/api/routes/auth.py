"""
app/api/routes/auth.py
"""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

import phonenumbers
from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, decode_token, generate_otp, hash_otp, verify_otp, hash_pin, verify_pin
from app.db.session import get_db
from app.models.models import OTPRecord, PushToken, RefreshToken, User, UserRole
from app.services.crud import get_user_by_id, get_user_by_phone, create_user
from app.services.cache import otp_rate_limiter
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


class RequestOTPBody(BaseModel):
    phone_number: str
    name: str = ""            # required for new registrations
    role: str = "passenger"   # passengers self-register; drivers use invite flow


class VerifyOTPBody(BaseModel):
    phone_number: str
    otp: str
    pin: Optional[str] = None   # required for admin accounts; ignored otherwise


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    role: str
    user_id: str
    name: str = ""
    must_change_password: bool = False   # driver first-login flag


@router.post("/request-otp")
async def request_otp(body: RequestOTPBody, db: AsyncSession = Depends(get_db)):
    """
    Send a 6-digit OTP to the given phone number.
    Rate limited to 5 attempts per 10 minutes per phone number.
    """
    # Validate and normalize phone number
    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError()
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    # Check rate limit
    if await otp_rate_limiter.is_blocked(normalized):
        raise HTTPException(status_code=429, detail="Too many OTP requests. Please wait 10 minutes.")

    await otp_rate_limiter.increment(normalized)

    # Get or create user
    user = await get_user_by_phone(phone_number=normalized, db=db)

    if not user:
        if not body.name:
            raise HTTPException(status_code=400, detail="Name is required for new registrations")

        if body.role not in [r.value for r in UserRole]:
            raise HTTPException(status_code=400, detail="Invalid role")

        # Drivers can only be created via admin onboarding (invite code)
        if body.role == UserRole.DRIVER.value:
            raise HTTPException(status_code=403, detail="Driver registration requires an invite code from admin")

        user = await create_user(
            phone_number=normalized,
            name=body.name,
            role=body.role,
            db=db,
        )

    # Generate and store OTP
    otp = generate_otp()
    otp_record = OTPRecord(
        user_id=user.id,
        otp_hash=hash_otp(otp),
        purpose="login",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(otp_record)
    await db.commit()

    # Send OTP via SMS (Africa's Talking)
    # In development, log it; in production, send via SMS service
    if settings.is_development:
        logger.info("DEV_OTP", phone=normalized, otp=otp)
    else:
        from app.services.sms import sms_service
        await sms_service.send(phone=normalized, message=f"Your Kabale Transport code is: {otp}. Valid for 10 minutes.")

    return {"message": "OTP sent", "phone_number": normalized}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp_endpoint(body: VerifyOTPBody, db: AsyncSession = Depends(get_db)):
    """Verify OTP and return JWT tokens."""
    from sqlalchemy import select, desc
    import uuid as uuid_lib

    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    user = await get_user_by_phone(phone_number=normalized, db=db)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Find the most recent valid OTP record
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OTPRecord).where(
            OTPRecord.user_id == user.id,
            OTPRecord.purpose == "login",
            OTPRecord.expires_at > now,
            OTPRecord.used_at.is_(None),
        ).order_by(desc(OTPRecord.created_at)).limit(1)
    )
    otp_record = result.scalar_one_or_none()

    if not otp_record or not verify_otp(body.otp, otp_record.otp_hash):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # Admin accounts require a PIN as a second factor on top of the OTP.
    if user.role == UserRole.ADMIN:
        if not body.pin or len(body.pin) < 4:
            raise HTTPException(status_code=400, detail="A 4-6 digit PIN is required for admin accounts")
        if user.pin_hash is None:
            user.pin_hash = hash_pin(body.pin)
        elif not verify_pin(body.pin, user.pin_hash):
            raise HTTPException(status_code=401, detail="Incorrect PIN")

    # Mark OTP as used
    otp_record.used_at = now

    # Mark user as verified
    user.is_verified = True
    user.last_seen_at = now

    # Create tokens
    _role_str = user.role.value if hasattr(user.role, 'value') else str(user.role)
    access_token = create_access_token(subject=str(user.id), role=_role_str)
    refresh_token = create_refresh_token(subject=str(user.id), role=_role_str)

    # Store refresh token hash
    from app.core.security import hash_otp as hash_token
    from app.models.models import RefreshToken
    from jose import jwt as jose_jwt
    from app.core.config import settings as _settings

    payload = jose_jwt.decode(refresh_token, _settings.APP_SECRET_KEY, algorithms=[_settings.JWT_ALGORITHM])
    refresh_record = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(refresh_token),
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )
    db.add(refresh_record)

    await otp_rate_limiter.reset(normalized)
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=_role_str,
        user_id=str(user.id),
        name=user.name,
    )




# ════════════════════════════════════════════════════════════════════════════════
# Password-based authentication (replaces OTP for passengers and drivers)
# ════════════════════════════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    phone_number: str
    name: str
    password: str = Field(..., min_length=6, description="At least 6 characters")


class LoginBody(BaseModel):
    phone_number: str
    password: str


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6)


async def _build_token_response(user, db: AsyncSession) -> TokenResponse:
    """Create JWT pair, persist refresh token, return TokenResponse."""
    from datetime import datetime, timezone
    from app.core.security import hash_otp as _hash_token
    from app.models.models import RefreshToken
    from jose import jwt as _jwt
    from app.core.config import settings as _s

    now = datetime.now(timezone.utc)
    user.is_verified = True
    user.last_seen_at = now

    _role_str = user.role.value if hasattr(user.role, 'value') else str(user.role)
    access_token = create_access_token(subject=str(user.id), role=_role_str)
    refresh_token = create_refresh_token(subject=str(user.id), role=_role_str)

    payload = _jwt.decode(refresh_token, _s.APP_SECRET_KEY, algorithms=[_s.JWT_ALGORITHM])
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(refresh_token),
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    ))
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=_role_str,
        user_id=str(user.id),
        name=user.name,
        must_change_password=bool(getattr(user, "must_change_password", False)),
    )


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterBody, db: AsyncSession = Depends(get_db)):
    """Passenger self-registration with phone + name + password."""
    from app.core.security import hash_password as _hp
    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    existing = await get_user_by_phone(phone_number=normalized, db=db)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this number already exists. Please log in.")

    user = await create_user(
        phone_number=normalized,
        name=body.name.strip(),
        role="passenger",
        db=db,
        password_hash=_hp(body.password),
    )
    return await _build_token_response(user, db)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody, db: AsyncSession = Depends(get_db)):
    """Universal login for passengers, drivers, and admins."""
    from app.core.security import verify_password as _vp
    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    user = await get_user_by_phone(phone_number=normalized, db=db)
    if not user:
        raise HTTPException(status_code=401, detail="No account found with this number")

    if not user.password_hash:
        raise HTTPException(
            status_code=401,
            detail="This account was set up before the password system. "
                   "Please contact the administrator to reset your access.",
        )

    if not _vp(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Your account has been suspended. Contact the administrator.")

    return await _build_token_response(user, db)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Change password. Required for drivers on first login."""
    from app.core.security import verify_password as _vp, hash_password as _hp
    from sqlalchemy import update as _update

    if not current_user.password_hash or not _vp(body.old_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    await db.execute(
        _update(User).where(User.id == current_user.id).values(
            password_hash=_hp(body.new_password),
            must_change_password=False,
        )
    )
    await db.commit()
    return {"message": "Password updated successfully"}



# ── Forgot / Reset Password ───────────────────────────────────────────────────

class ForgotPasswordBody(BaseModel):
    phone_number: str


class ResetPasswordBody(BaseModel):
    phone_number: str
    code: str
    new_password: str = Field(..., min_length=6)


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordBody, db: AsyncSession = Depends(get_db)):
    """
    Generate a 6-digit reset code and return it in the response.
    The user is on their own device so showing the code is safe.
    Code expires in 60 minutes.
    """
    import random
    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    user = await get_user_by_phone(phone_number=normalized, db=db)
    if not user:
        # Don't reveal whether account exists
        raise HTTPException(status_code=404, detail="No account found with this number")

    code = str(random.randint(100000, 999999))
    redis = await otp_rate_limiter._get_redis() if hasattr(otp_rate_limiter, '_get_redis') else None

    from app.services.cache import get_redis as _get_redis
    redis = await _get_redis()
    await redis.setex(f"pwd_reset:{normalized}", 3600, code)

    return {
        "code": code,
        "message": "Copy this code and use it to reset your password. It expires in 60 minutes.",
        "expires_minutes": 60,
    }


@router.post("/reset-password")
async def reset_password(body: ResetPasswordBody, db: AsyncSession = Depends(get_db)):
    """Verify reset code and set new password."""
    from app.core.security import hash_password as _hp
    from sqlalchemy import update as _update
    from app.services.cache import get_redis as _get_redis

    try:
        parsed = phonenumbers.parse(body.phone_number, "UG")
        normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    redis = await _get_redis()
    stored = await redis.get(f"pwd_reset:{normalized}")

    if not stored or stored != body.code:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")

    user = await get_user_by_phone(phone_number=normalized, db=db)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        _update(User).where(User.id == user.id).values(
            password_hash=_hp(body.new_password),
            must_change_password=False,
        )
    )
    await db.commit()
    await redis.delete(f"pwd_reset:{normalized}")

    return {"message": "Password reset successfully. Please log in with your new password."}

# ── Token Refresh ────────────────────────────────────────────────────────────

class RefreshBody(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(body: RefreshBody, db: AsyncSession = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access/refresh pair.
    Rotates the refresh token on every use: the old one is revoked and a new
    one is issued, so a stolen-but-unused refresh token can't be replayed
    indefinitely once the legitimate device refreshes again.
    """
    from sqlalchemy import select
    from jose import jwt as jose_jwt

    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    jti = payload.get("jti")
    user_id = payload.get("sub")
    if not jti or not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    result = await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    stored = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    stored_expires_at = stored.expires_at if stored else None
    if stored_expires_at is not None and stored_expires_at.tzinfo is None:
        stored_expires_at = stored_expires_at.replace(tzinfo=timezone.utc)

    if (
        not stored
        or stored.revoked_at is not None
        or stored_expires_at <= now
        or stored.token_hash != hash_otp(body.refresh_token)
    ):
        raise HTTPException(status_code=401, detail="Refresh token is no longer valid")

    user = await get_user_by_id(user_id=uuid.UUID(user_id), db=db)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is inactive")

    # Rotate: revoke the old token, issue a fresh pair
    stored.revoked_at = now

    new_access = create_access_token(subject=str(user.id), role=user.role.value)
    new_refresh = create_refresh_token(subject=str(user.id), role=user.role.value)
    new_payload = jose_jwt.decode(new_refresh, settings.APP_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_otp(new_refresh),
        jti=new_payload["jti"],
        expires_at=datetime.fromtimestamp(new_payload["exp"], tz=timezone.utc),
    ))
    await db.commit()

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        role=_role_str,
        user_id=str(user.id),
    )


# ── Logout ─────────────────────────────────────────────────────────────────

class LogoutBody(BaseModel):
    refresh_token: str
    fcm_token: Optional[str] = None   # pass the device's push token to stop notifications too


@router.post("/logout")
async def logout(
    body: LogoutBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Revoke the given refresh token and, optionally, deactivate this device's push token."""
    try:
        payload = decode_token(body.refresh_token)
        jti = payload.get("jti")
    except JWTError:
        jti = None

    if jti:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.user_id == current_user.id)
            .values(revoked_at=datetime.now(timezone.utc))
        )

    if body.fcm_token:
        await db.execute(
            update(PushToken)
            .where(PushToken.user_id == current_user.id, PushToken.fcm_token == body.fcm_token)
            .values(is_active=False)
        )

    await db.commit()
    return {"message": "Logged out"}


# ── Push Notification Device Tokens ─────────────────────────────────────────

class RegisterPushTokenBody(BaseModel):
    fcm_token: str
    device_type: str = "android"   # android | ios


@router.post("/push-token")
async def register_push_token(
    body: RegisterPushTokenBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Register (or refresh) this device's FCM token for the current user.
    Safe to call every time the app starts - upserts on (user_id, fcm_token),
    the same unique constraint already defined on the PushToken table.
    """
    stmt = pg_insert(PushToken).values(
        id=uuid.uuid4(),
        user_id=current_user.id,
        fcm_token=body.fcm_token,
        device_type=body.device_type,
        is_active=True,
    ).on_conflict_do_update(
        index_elements=["user_id", "fcm_token"],
        set_={"is_active": True, "device_type": body.device_type, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)
    await db.commit()
    return {"message": "Push token registered"}


@router.delete("/push-token")
async def unregister_push_token(
    fcm_token: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a device's push token, e.g. when the user disables notifications."""
    await db.execute(
        update(PushToken)
        .where(PushToken.user_id == current_user.id, PushToken.fcm_token == fcm_token)
        .values(is_active=False)
    )
    await db.commit()
    return {"message": "Push token unregistered"}
