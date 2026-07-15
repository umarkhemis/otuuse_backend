"""
app/api/routes/admin.py
------------------------
Admin-only endpoints for platform management.
All routes require role=admin JWT token.

Covers:
- Driver onboarding and management
- Delivery request handling and admin-to-passenger relay
- Platform overview dashboard data
- Driver strike management
"""

import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_admin
from app.core.security import hash_pin
from app.db.session import get_db
from app.models.models import (
    User, DriverProfile, Ride, Delivery, Message, DriverStrike,
    UserRole, DriverAvailability, DeliveryStatus, RideStatus,
    MessageRole, MessageIntent, TransactionType, TransactionStatus
)
from app.services.crud import issue_driver_strike
from app.services.audit import log_admin_action
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


# ── Driver Onboarding ──────────────────────────────────────────────────────────

class OnboardDriverBody(BaseModel):
    phone_number: str
    name: str
    initial_pin: str = Field(..., min_length=4, max_length=6)
    subscription_months: int = Field(default=1, ge=1, le=12)
    plate_number: Optional[str] = Field(default=None, max_length=20)


@router.post("/drivers/onboard")
async def onboard_driver(
    body: OnboardDriverBody,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Admin creates a new driver account.
    Generates an invite code and sets an initial PIN.
    Driver logs in with their phone + OTP, then sets their own PIN.
    """
    import phonenumbers as pn

    try:
        parsed = pn.parse(body.phone_number, "UG")
        normalized = pn.format_number(parsed, pn.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    # Check if user already exists
    result = await db.execute(select(User).where(User.phone_number == normalized))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="A user with this phone number already exists")

    # Create user account
    user = User(
        id=uuid.uuid4(),
        phone_number=normalized,
        name=body.name,
        role=UserRole.DRIVER,
        is_active=True,
        is_verified=True,   # admin-verified at onboarding
        wallet_balance_ugx=0,
    )
    db.add(user)
    await db.flush()

    # Generate invite code
    invite_code = secrets.token_hex(4).upper()

    # Calculate subscription expiry
    subscription_expires = datetime.now(timezone.utc) + timedelta(days=30 * body.subscription_months)

    # Create driver profile
    driver_profile = DriverProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        availability=DriverAvailability.OFFLINE,
        rating=5.0,
        total_rides=0,
        total_earnings_ugx=0,
        subscription_active=True,
        subscription_expires_at=subscription_expires,
        pin_hash=hash_pin(body.initial_pin),
        invite_code=invite_code,
        plate_number=body.plate_number,
        onboarded_at=datetime.now(timezone.utc),
        onboarded_by_admin_id=current_admin.id,
        is_documents_verified=False,
    )
    db.add(driver_profile)
    await log_admin_action(
        db, admin_id=current_admin.id, action="onboard_driver",
        target_type="user", target_id=str(user.id),
        details=f"name={body.name}, phone={normalized}",
    )
    await db.commit()

    logger.info("driver_onboarded", driver_id=str(user.id), admin_id=str(current_admin.id))

    return {
        "driver_id": str(user.id),
        "phone_number": normalized,
        "invite_code": invite_code,
        "subscription_expires_at": subscription_expires.isoformat(),
        "message": f"Driver {body.name} onboarded successfully. Share the invite code with them.",
    }


@router.get("/drivers")
async def list_drivers(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    online_only: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    """List all drivers with their current status."""
    query = (
        select(User, DriverProfile)
        .join(DriverProfile, DriverProfile.user_id == User.id)
        .where(User.role == UserRole.DRIVER)
    )

    if online_only:
        query = query.where(DriverProfile.availability == DriverAvailability.ONLINE)

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    rows = result.fetchall()

    return [
        {
            "user_id": str(user.id),
            "name": user.name,
            "phone_number": user.phone_number,
            "is_active": user.is_active,
            "availability": profile.availability.value,
            "rating": profile.rating,
            "total_rides": profile.total_rides,
            "subscription_active": profile.subscription_active,
            "subscription_expires_at": profile.subscription_expires_at.isoformat() if profile.subscription_expires_at else None,
            "wallet_balance_ugx": user.wallet_balance_ugx,
            "documents_verified": profile.is_documents_verified,
            "plate_number": profile.plate_number,
        }
        for user, profile in rows
    ]


@router.patch("/drivers/{driver_id}/suspend")
async def suspend_driver(
    driver_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Immediately suspend a driver. They go offline and cannot receive rides."""
    driver_user = await db.get(User, uuid.UUID(driver_id))
    if not driver_user or driver_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=404, detail="Driver not found")

    await db.execute(
        update(User).where(User.id == driver_user.id).values(is_active=False)
    )
    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.user_id == driver_user.id)
        .values(availability=DriverAvailability.OFFLINE, subscription_active=False)
    )
    await log_admin_action(
        db, admin_id=current_admin.id, action="suspend_driver",
        target_type="user", target_id=driver_id,
    )
    await db.commit()

    logger.info("driver_suspended", driver_id=driver_id, admin_id=str(current_admin.id))
    return {"message": f"Driver {driver_user.name} has been suspended"}


@router.patch("/drivers/{driver_id}/reinstate")
async def reinstate_driver(
    driver_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Reinstate a suspended driver."""
    driver_user = await db.get(User, uuid.UUID(driver_id))
    if not driver_user or driver_user.role != UserRole.DRIVER:
        raise HTTPException(status_code=404, detail="Driver not found")

    profile_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == driver_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    # Only reinstate if subscription is still valid
    is_subscription_valid = (
        profile.subscription_expires_at
        and profile.subscription_expires_at > datetime.now(timezone.utc)
    )

    await db.execute(
        update(User).where(User.id == driver_user.id).values(is_active=True)
    )
    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.user_id == driver_user.id)
        .values(subscription_active=is_subscription_valid)
    )
    await log_admin_action(
        db, admin_id=current_admin.id, action="reinstate_driver",
        target_type="user", target_id=driver_id,
    )
    await db.commit()

    return {
        "message": f"Driver {driver_user.name} reinstated",
        "subscription_active": is_subscription_valid,
    }


@router.post("/drivers/{driver_id}/renew-subscription")
async def renew_subscription(
    driver_id: str,
    months: int,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Manually renew a driver's subscription after they pay."""
    profile_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == uuid.UUID(driver_id))
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    now = datetime.now(timezone.utc)
    # Extend from current expiry or from now, whichever is later
    base = max(profile.subscription_expires_at or now, now)
    new_expiry = base + timedelta(days=30 * months)

    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.id == profile.id)
        .values(
            subscription_active=True,
            subscription_expires_at=new_expiry,
        )
    )
    await log_admin_action(
        db, admin_id=current_admin.id, action="renew_subscription",
        target_type="user", target_id=driver_id, details=f"months={months}",
    )
    await db.commit()

    return {
        "message": f"Subscription renewed for {months} month(s)",
        "new_expiry": new_expiry.isoformat(),
    }


@router.post("/drivers/{driver_id}/strike")
async def add_strike(
    driver_id: str,
    reason: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    notes: str = "",
):
    """Manually issue a strike to a driver."""
    await issue_driver_strike(
        db=db,
        driver_user_id=uuid.UUID(driver_id),
        reason=reason,
        notes=notes,
        admin_id=current_admin.id,
    )
    await log_admin_action(
        db, admin_id=current_admin.id, action="issue_strike",
        target_type="user", target_id=driver_id, details=f"reason={reason}",
    )
    await db.commit()
    return {"message": "Strike issued"}


# ── Driver Document Verification ──────────────────────────────────────────────

@router.get("/drivers/{driver_id}/documents")
async def get_driver_documents(
    driver_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin views a driver's uploaded verification documents."""
    profile_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == uuid.UUID(driver_id))
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    from app.services.storage import storage_service

    async def _url(key: Optional[str]) -> Optional[str]:
        return await storage_service.get_url(key) if key else None

    return {
        "is_documents_verified": profile.is_documents_verified,
        "national_id_url": await _url(profile.national_id_doc),
        "license_url": await _url(profile.license_doc),
        "registration_url": await _url(profile.registration_doc),
    }


@router.patch("/drivers/{driver_id}/verify-documents")
async def verify_driver_documents(
    driver_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin approves a driver's documents after manual review."""
    profile_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == uuid.UUID(driver_id))
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    if not all([profile.national_id_doc, profile.license_doc, profile.registration_doc]):
        raise HTTPException(status_code=400, detail="All three documents must be uploaded before verification")

    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.id == profile.id)
        .values(is_documents_verified=True)
    )
    await log_admin_action(
        db, admin_id=current_admin.id, action="verify_documents",
        target_type="user", target_id=driver_id,
    )
    await db.commit()

    logger.info("driver_documents_verified", driver_id=driver_id, admin_id=str(current_admin.id))
    return {"message": "Documents verified"}


# ── Delivery Management ────────────────────────────────────────────────────────

class AdminDeliveryReplyBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    new_status: Optional[str] = None   # negotiating | confirmed | cancelled


@router.get("/deliveries")
async def list_deliveries(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
):
    """List all delivery requests, newest first."""
    query = select(Delivery).order_by(Delivery.created_at.desc())

    if status_filter:
        try:
            query = query.where(Delivery.status == DeliveryStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status_filter}")

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    deliveries = result.scalars().all()

    return [
        {
            "id": str(d.id),
            "passenger_id": str(d.passenger_id),
            "pickup_name": d.pickup_name,
            "dropoff_name": d.dropoff_name,
            "item_description": d.item_description,
            "is_urgent": d.is_urgent,
            "agreed_fare_ugx": d.agreed_fare_ugx,
            "status": d.status.value,
            "admin_notes": d.admin_notes,
            "created_at": d.created_at.isoformat(),
        }
        for d in deliveries
    ]


@router.get("/deliveries/{delivery_id}/messages")
async def get_delivery_conversation(
    delivery_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Get the full conversation thread for a delivery request.
    Admin reads this to understand what the passenger needs.
    """
    delivery = await db.get(Delivery, uuid.UUID(delivery_id))
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    result = await db.execute(
        select(Message)
        .where(Message.delivery_id == delivery.id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()

    return {
        "delivery": {
            "id": str(delivery.id),
            "pickup_name": delivery.pickup_name,
            "dropoff_name": delivery.dropoff_name,
            "item_description": delivery.item_description,
            "is_urgent": delivery.is_urgent,
            "status": delivery.status.value,
        },
        "messages": [
            {
                "role": m.role.value,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    }


@router.post("/deliveries/{delivery_id}/reply")
async def reply_to_delivery(
    delivery_id: str,
    body: AdminDeliveryReplyBody,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Admin sends a message to the passenger via the delivery thread.
    The agent relays this as if it came from the platform.

    This is the core of the admin-supervised delivery flow:
    admin types here -> agent voices it to the passenger.
    """
    delivery = await db.get(Delivery, uuid.UUID(delivery_id))
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # Save admin message to the conversation
    admin_message = Message(
        id=uuid.uuid4(),
        user_id=current_admin.id,
        role=MessageRole.ADMIN,
        content=body.message,
        intent=MessageIntent.NONE,
        delivery_id=delivery.id,
    )
    db.add(admin_message)

    # Update delivery status if provided
    if body.new_status:
        try:
            new_status = DeliveryStatus(body.new_status)
            updates = {"status": new_status, "admin_id": current_admin.id}
            if new_status in [DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED]:
                updates["resolved_at"] = datetime.now(timezone.utc)
            await db.execute(
                update(Delivery).where(Delivery.id == delivery.id).values(**updates)
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {body.new_status}")

    await log_admin_action(
        db, admin_id=current_admin.id, action="delivery_reply",
        target_type="delivery", target_id=delivery_id, details=body.message[:200],
    )
    await db.commit()

    # Now relay the admin's message to the passenger via the agent
    # The agent voices it naturally so it doesn't feel robotic
    from app.services.agent.llm_client import llm_client
    from app.services.agent.system_prompt import build_system_prompt

    relay_prompt = (
        f"The admin has provided the following response to the passenger's delivery request: "
        f'"{body.message}". '
        "Relay this to the passenger in a warm, natural way as the platform's voice. "
        "Do not add information that wasn't in the admin's message. Keep it concise."
    )

    agent_reply = await llm_client.complete(
        system_prompt=build_system_prompt(context_note=relay_prompt),
        messages=[{"role": "user", "content": "What's the update on my delivery?"}],
        max_tokens=300,
    )

    # Save agent's relay message to the passenger's conversation
    agent_message = Message(
        id=uuid.uuid4(),
        user_id=delivery.passenger_id,
        role=MessageRole.AGENT,
        content=agent_reply,
        intent=MessageIntent.DELIVERY_REQUEST,
        delivery_id=delivery.id,
    )
    db.add(agent_message)
    await db.commit()

    # Send push notification to passenger
    from app.services.notifications import notification_service
    from app.models.models import PushToken
    passenger_tokens_result = await db.execute(
        select(PushToken.fcm_token).where(
            PushToken.user_id == delivery.passenger_id,
            PushToken.is_active == True,
        )
    )
    tokens = [row[0] for row in passenger_tokens_result.fetchall()]
    if tokens:
        from firebase_admin import messaging
        msg = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(
                title="Delivery Update",
                body=agent_reply[:100],
            ),
            data={"type": "delivery_update", "delivery_id": delivery_id},
        )
        messaging.send_each_for_multicast(msg)

    return {
        "message": "Reply sent to passenger",
        "agent_relay": agent_reply,
    }



# ── Admin Document Upload ─────────────────────────────────────────────────────

@router.post("/drivers/{driver_id}/upload-document")
async def admin_upload_driver_document(
    driver_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    doc_type: str = Form(...),   # national_id | license | registration
    file: UploadFile = File(...),
):
    """
    Admin uploads a verification document on behalf of a driver.
    Useful during in-person onboarding where admin collects documents directly.
    Re-uploading resets is_documents_verified - admin must re-verify.
    """
    from fastapi import File, Form, UploadFile

    valid_types = {"national_id", "license", "registration"}
    if doc_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"doc_type must be one of {sorted(valid_types)}"
        )

    allowed_extensions = {".jpg", ".jpeg", ".png", ".pdf"}
    ext = (
        "." + file.filename.rsplit(".", 1)[-1].lower()
        if file.filename and "." in file.filename
        else ""
    )
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, PNG, or PDF files are accepted"
        )

    profile_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == uuid.UUID(driver_id))
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    content_bytes = await file.read()
    from app.core.config import settings as _settings
    max_bytes = _settings.STORAGE_MAX_UPLOAD_MB * 1024 * 1024
    if len(content_bytes) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {_settings.STORAGE_MAX_UPLOAD_MB}MB limit"
        )

    from app.services.storage import storage_service
    key = await storage_service.upload(
        driver_user_id=driver_id,
        doc_type=doc_type,
        filename=file.filename,
        content=content_bytes,
    )

    column_map = {
        "national_id": "national_id_doc",
        "license": "license_doc",
        "registration": "registration_doc",
    }
    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.id == profile.id)
        .values(**{column_map[doc_type]: key, "is_documents_verified": False})
    )
    await log_admin_action(
        db,
        admin_id=current_admin.id,
        action="upload_driver_document",
        target_type="driver",
        target_id=driver_id,
        details=f"doc_type={doc_type}",
    )
    await db.commit()

    return {
        "message": f"{doc_type.replace('_', ' ')} uploaded by admin. Pending verification.",
        "storage_key": key,
    }

# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def get_dashboard(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Platform overview stats for the admin dashboard.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Drivers online right now
    online_result = await db.execute(
        select(func.count(DriverProfile.id)).where(
            DriverProfile.availability == DriverAvailability.ONLINE
        )
    )
    drivers_online = online_result.scalar() or 0

    # Active rides right now
    active_rides_result = await db.execute(
        select(func.count(Ride.id)).where(
            Ride.status.in_([
                RideStatus.MATCHED, RideStatus.ACCEPTED,
                RideStatus.DRIVER_ARRIVING, RideStatus.IN_PROGRESS
            ])
        )
    )
    active_rides = active_rides_result.scalar() or 0

    # Rides completed today
    today_rides_result = await db.execute(
        select(func.count(Ride.id)).where(
            Ride.status == RideStatus.PAID,
            Ride.completed_at >= today_start,
        )
    )
    rides_today = today_rides_result.scalar() or 0

    # Revenue today (commission collected)
    from app.models.models import Transaction
    revenue_result = await db.execute(
        select(func.sum(Transaction.amount_ugx)).where(
            Transaction.type == TransactionType.COMMISSION,
            Transaction.status == TransactionStatus.COMPLETED,
            Transaction.created_at >= today_start,
        )
    )
    revenue_today = revenue_result.scalar() or 0

    # Pending deliveries
    pending_deliveries_result = await db.execute(
        select(func.count(Delivery.id)).where(
            Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.UNDER_REVIEW])
        )
    )
    pending_deliveries = pending_deliveries_result.scalar() or 0

    # Total active drivers (subscription active)
    total_active_result = await db.execute(
        select(func.count(DriverProfile.id)).where(
            DriverProfile.subscription_active == True
        )
    )
    total_active_drivers = total_active_result.scalar() or 0

    return {
        "drivers_online_now": drivers_online,
        "total_active_drivers": total_active_drivers,
        "active_rides_now": active_rides,
        "rides_completed_today": rides_today,
        "revenue_today_ugx": revenue_today,
        "pending_delivery_requests": pending_deliveries,
        "generated_at": now.isoformat(),
    }


# ── Ride Oversight ─────────────────────────────────────────────────────────────

@router.get("/rides")
async def list_rides(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List rides with optional status filter."""
    query = select(Ride).order_by(Ride.requested_at.desc())

    if status_filter:
        try:
            query = query.where(Ride.status == RideStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status_filter}")

    result = await db.execute(query.limit(limit).offset(offset))
    rides = result.scalars().all()

    return [
        {
            "id": str(r.id),
            "passenger_id": str(r.passenger_id),
            "driver_id": str(r.driver_id) if r.driver_id else None,
            "pickup_name": r.pickup_name,
            "dropoff_name": r.dropoff_name,
            "status": r.status.value,
            "estimated_fare_ugx": r.estimated_fare_ugx,
            "final_fare_ugx": r.final_fare_ugx,
            "commission_ugx": r.commission_ugx,
            "requested_at": r.requested_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in rides
    ]


@router.get("/rides/{ride_id}/trail")
async def get_ride_gps_trail(
    ride_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Get the full GPS trail for a ride.
    Used for dispute resolution - admin can see exactly where the driver went.
    """
    from app.models.models import GPSTrailPoint
    from geoalchemy2.shape import to_shape

    result = await db.execute(
        select(GPSTrailPoint)
        .where(GPSTrailPoint.ride_id == uuid.UUID(ride_id))
        .order_by(GPSTrailPoint.recorded_at.asc())
    )
    points = result.scalars().all()

    return {
        "ride_id": ride_id,
        "point_count": len(points),
        "trail": [
            {
                "latitude": to_shape(p.location).y,
                "longitude": to_shape(p.location).x,
                "speed_kmh": p.speed_kmh,
                "accuracy_meters": p.accuracy_meters,
                "recorded_at": p.recorded_at.isoformat(),
            }
            for p in points
        ],
    }



@router.post("/deliveries/{delivery_id}/photo")
async def admin_upload_delivery_photo(
    delivery_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
):
    """
    Admin uploads a photo related to the delivery
    (e.g. photo of item before pickup, or proof of delivery).
    URL is stored on the delivery and returned to the passenger via polling.
    """
    from app.services.storage import storage_service, StorageError
    from sqlalchemy import update as _update

    delivery = await db.get(Delivery, uuid.UUID(delivery_id))
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    ext = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, or WebP images are accepted")

    from app.core.config import settings as _s
    content_bytes = await file.read()
    if len(content_bytes) > _s.STORAGE_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File exceeds {_s.STORAGE_MAX_UPLOAD_MB}MB limit")

    try:
        url = await storage_service.upload_photo(
            content=content_bytes,
            filename=file.filename or f"photo{ext}",
            folder="delivery-admin-photos",
        )
    except StorageError as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    await db.execute(
        _update(Delivery).where(Delivery.id == delivery.id).values(admin_photo_url=url)
    )
    await log_admin_action(
        db,
        admin_id=current_admin.id,
        action="upload_delivery_photo",
        target_type="delivery",
        target_id=delivery_id,
    )
    await db.commit()

    return {"url": url, "message": "Photo uploaded"}

# ── Audit Log ───────────────────────────────────────────────────────────────

@router.get("/audit-log")
async def get_audit_log(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """Chronological log of every admin action - who did what, when."""
    from app.models.models import AdminAuditLog

    result = await db.execute(
        select(AdminAuditLog, User.name)
        .join(User, User.id == AdminAuditLog.admin_id)
        .order_by(AdminAuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.fetchall()

    return [
        {
            "id": str(entry.id),
            "admin_id": str(entry.admin_id),
            "admin_name": admin_name,
            "action": entry.action,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
            "details": entry.details,
            "created_at": entry.created_at.isoformat(),
        }
        for entry, admin_name in rows
    ]


# ── PIN Management ─────────────────────────────────────────────────────────

class ChangePinBody(BaseModel):
    current_pin: str
    new_pin: str = Field(..., min_length=4, max_length=6)


@router.post("/me/change-pin")
async def change_own_pin(
    body: ChangePinBody,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin rotates their own login PIN."""
    from app.core.security import verify_pin, hash_pin

    if not current_admin.pin_hash or not verify_pin(body.current_pin, current_admin.pin_hash):
        raise HTTPException(status_code=401, detail="Current PIN is incorrect")

    await db.execute(
        update(User).where(User.id == current_admin.id).values(pin_hash=hash_pin(body.new_pin))
    )
    await log_admin_action(db, admin_id=current_admin.id, action="change_pin")
    await db.commit()

    return {"message": "PIN updated"}
