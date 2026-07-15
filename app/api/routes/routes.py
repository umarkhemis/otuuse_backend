"""
app/api/routes/chat.py - Passenger chat endpoint
"""
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_passenger
from app.db.session import get_db
from app.models.models import User
from app.services.agent.agent import agent_service
from app.services.dispatch import dispatch_service
from app.services.cache import get_redis

router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatMessageBody(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    intent: str
    ride_id: str | None = None
    delivery_id: str | None = None
    fare_ugx: int | None = None
    driver_name: str | None = None
    driver_phone: str | None = None
    driver_plate: str | None = None


@router.post("/message", response_model=ChatResponse)
async def send_message(
    body: ChatMessageBody,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """
    Main passenger chat endpoint.
    Forwards message to AI agent, returns agent response.
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if len(body.message) > 1000:
        raise HTTPException(status_code=400, detail="Message too long")

    response = await agent_service.process_message(
        user_id=current_user.id,
        user_message=body.message.strip(),
        db=db,
        user_name=current_user.name,
    )

    return ChatResponse(
        reply=response.message,
        intent=response.intent.value,
        ride_id=str(response.ride_id) if response.ride_id else None,
        delivery_id=str(response.delivery_id) if response.delivery_id else None,
        fare_ugx=response.fare_ugx,
        driver_name=response.driver_name,
        driver_phone=response.driver_phone,
        driver_plate=response.driver_plate,
    )


class ConfirmRideBody(BaseModel):
    confirmed: bool
    ride_id: str | None = None   # required in the new driver-first flow


@router.post("/confirm-ride")
async def confirm_ride(
    body: ConfirmRideBody,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """
    Called after the driver accepts and the passenger taps Confirm on the fare card.
    The ride already exists and is in ACCEPTED status at this point.
    This endpoint simply notifies the driver that the passenger is ready.
    """
    from app.models.models import Ride, RideStatus
    from uuid import UUID as _UUID

    if not body.confirmed:
        # Passenger cancelled after driver accepted - cancel the ride
        if body.ride_id:
            from datetime import datetime as _dt, timezone as _tz
            from sqlalchemy import update as _update
            await db.execute(
                _update(Ride)
                .where(Ride.id == _UUID(body.ride_id), Ride.passenger_id == current_user.id)
                .values(
                    status=RideStatus.CANCELLED,
                    cancellation_reason="passenger_cancelled_after_driver_accepted",
                    cancelled_at=_dt.now(_tz.utc),
                )
            )
            await db.commit()
        return {"message": "Ride cancelled"}

    if not body.ride_id:
        raise HTTPException(status_code=400, detail="ride_id is required")

    ride = await db.get(Ride, _UUID(body.ride_id))
    if not ride or ride.passenger_id != current_user.id:
        raise HTTPException(status_code=404, detail="Ride not found")

    if ride.status != RideStatus.ACCEPTED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot confirm: ride is in '{ride.status.value}' status. "
                "Wait for the driver to accept first."
            ),
        )

    # Notify driver that passenger confirmed and to proceed to pickup
    from app.services.notifications import notification_service
    await notification_service.notify_driver_passenger_confirmed(
        driver_user_id=ride.driver_id,
        ride_id=ride.id,
        db=db,
    )

    return {
        "ride_id": str(ride.id),
        "message": "Confirmed! Your driver is heading to your pickup point.",
    }


@router.get("/ride-status/{ride_id}")
async def get_ride_status(
    ride_id: str,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """
    Passenger polls this every 4 seconds after requesting a ride.
    Returns status and driver details once the driver accepts.
    Flutter shows the fare card only when status == 'accepted'.
    """
    from app.models.models import Ride, RideStatus, DriverProfile
    from sqlalchemy import select as _select
    from uuid import UUID as _UUID

    ride = await db.get(Ride, _UUID(ride_id))
    if not ride or ride.passenger_id != current_user.id:
        raise HTTPException(status_code=404, detail="Ride not found")

    base: dict = {
        "status": ride.status.value,
        "fare_ugx": None,
        "distance_km": None,
        "driver_name": None,
        "driver_phone": None,
        "driver_plate": None,
    }

    if ride.status in [
        RideStatus.ACCEPTED,
        RideStatus.DRIVER_ARRIVING,
        RideStatus.IN_PROGRESS,
    ] and ride.driver_id:
        driver_user = await db.get(User, ride.driver_id)
        dp_result = await db.execute(
            _select(DriverProfile).where(DriverProfile.user_id == ride.driver_id)
        )
        dp = dp_result.scalar_one_or_none()
        base.update({
            "fare_ugx": ride.estimated_fare_ugx,
            "distance_km": ride.estimated_distance_km,
            "driver_name": driver_user.name if driver_user else "Your driver",
            "driver_phone": driver_user.phone_number if driver_user else "",
            "driver_plate": (dp.plate_number if dp else None) or "—",
        })

    return base



@router.get("/delivery-status/{delivery_id}")
async def get_delivery_status(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """
    Passenger polls this every 15 seconds after creating a delivery request.
    Returns the delivery status and the latest admin reply (if any).
    Flutter shows new replies as agent messages in the chat.
    """
    from app.models.models import Delivery, Message, MessageRole
    from sqlalchemy import select as _sel, desc as _desc
    from uuid import UUID as _UUID

    delivery = await db.get(Delivery, _UUID(delivery_id))
    if not delivery or delivery.passenger_id != current_user.id:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # Get the latest agent relay message for this delivery
    # (saved by the admin reply endpoint after LLM voices the admin message)
    result = await db.execute(
        _sel(Message)
        .where(
            Message.delivery_id == delivery.id,
            Message.role == MessageRole.ADMIN,  # Only admin replies, not initial agent messages
        )
        .order_by(_desc(Message.created_at))
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    return {
        "status": delivery.status.value,
        "admin_reply": latest.content if latest else None,
        "replied_at": latest.created_at.isoformat() if latest else None,
    }

# ────────────────────────────────────────────────────────────────────────────────
"""
app/api/routes/driver.py - Driver operations
"""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_driver
from app.core.config import settings
from app.db.session import get_db
from app.models.models import User, DriverProfile, DriverAvailability
from app.services.dispatch import dispatch_service
from app.services.cache import driver_location_store

driver_router = APIRouter(prefix="/driver", tags=["Driver"])


class LocationUpdateBody(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    speed_kmh: float = Field(default=0.0, ge=0)
    accuracy_meters: float = Field(default=10.0, ge=0)
    recorded_at: datetime


class AvailabilityBody(BaseModel):
    online: bool


class RideActionBody(BaseModel):
    action: str   # accept | decline | arrived | start | complete


@driver_router.post("/location")
async def update_location(
    body: LocationUpdateBody,
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """
    Receives GPS updates from the driver app every 3 seconds.
    Updates Redis (fast) and PostgreSQL (persistent).
    Triggers GPS state machine checks if driver has an active ride.
    """
    await dispatch_service.process_driver_location_update(
        driver_user_id=current_user.id,
        latitude=body.latitude,
        longitude=body.longitude,
        speed_kmh=body.speed_kmh,
        accuracy_meters=body.accuracy_meters,
        recorded_at=body.recorded_at,
        db=db,
    )
    return {"status": "ok"}


@driver_router.post("/availability")
async def set_availability(
    body: AvailabilityBody,
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """Toggle driver online/offline status."""
    from sqlalchemy import select

    driver_result = await db.execute(
        select(DriverProfile).where(DriverProfile.user_id == current_user.id)
    )
    driver = driver_result.scalar_one_or_none()

    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    if not driver.subscription_active:
        raise HTTPException(status_code=403, detail="Your subscription is inactive. Please renew to go online.")

    new_status = DriverAvailability.ONLINE if body.online else DriverAvailability.OFFLINE

    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .values(availability=new_status)
    )

    if not body.online:
        await driver_location_store.delete(str(current_user.id))

    await db.commit()
    return {"status": "online" if body.online else "offline"}


@driver_router.post("/ride/{ride_id}/action")
async def ride_action(
    ride_id: str,
    body: RideActionBody,
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """
    Driver taps Accept, Decline, Arrived, Start, or Complete.
    The GPS state machine also triggers these transitions automatically
    as a backup - this endpoint is the manual trigger.
    """
    from uuid import UUID as UUID_type
    from app.models.models import Ride, RideStatus
    from sqlalchemy import update as sa_update

    ride_uuid = UUID_type(ride_id)

    if body.action == "accept":
        success = await dispatch_service.handle_driver_acceptance(
            ride_id=ride_uuid,
            driver_user_id=current_user.id,
            db=db,
        )
        if not success:
            raise HTTPException(status_code=400, detail="Acceptance window has expired")
        return {"status": "accepted"}

    elif body.action == "decline":
        await dispatch_service.handle_driver_decline(
            ride_id=ride_uuid,
            driver_user_id=current_user.id,
            db=db,
        )
        return {"status": "declined"}

    elif body.action == "arrived":
        await db.execute(
            sa_update(Ride)
            .where(Ride.id == ride_uuid, Ride.driver_id == current_user.id)
            .values(status=RideStatus.DRIVER_ARRIVING, driver_arrived_at=datetime.now(timezone.utc))
        )
        await db.commit()
        from app.services.notifications import notification_service
        await notification_service.notify_passenger_driver_arrived(ride_id=ride_uuid, db=db)
        return {"status": "marked_arrived"}

    elif body.action == "start":
        await db.execute(
            sa_update(Ride)
            .where(Ride.id == ride_uuid, Ride.driver_id == current_user.id)
            .values(status=RideStatus.IN_PROGRESS, started_at=datetime.now(timezone.utc))
        )
        await db.commit()
        return {"status": "ride_started"}

    elif body.action == "complete":
        await dispatch_service.complete_ride(ride_id=ride_uuid, db=db)
        return {"status": "ride_completed"}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")


@driver_router.get("/earnings")
async def get_earnings(
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """Driver earnings summary."""
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.models.models import Transaction, TransactionType, TransactionStatus

    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(
            func.sum(Transaction.amount_ugx).label("total"),
        ).where(
            Transaction.user_id == current_user.id,
            Transaction.type == TransactionType.DRIVER_CREDIT,
            Transaction.status == TransactionStatus.COMPLETED,
        )
    )
    total_earnings = result.scalar() or 0

    return {
        "wallet_balance_ugx": current_user.wallet_balance_ugx,
        "total_earnings_ugx": total_earnings,
    }


@driver_router.get("/ride/active")
async def get_driver_active_ride(
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """
    Polling endpoint called every 4 seconds by the driver app.
    Returns the driver's current active ride (any status from MATCHED
    through IN_PROGRESS), or null if none. Used in place of FCM push
    notifications during development.
    """
    from app.services.crud import get_active_ride_for_driver
    ride = await get_active_ride_for_driver(user_id=current_user.id, db=db)
    if not ride:
        return {"ride": None}
    passenger = await db.get(User, ride.passenger_id)
    return {
        "ride": {
            "id": str(ride.id),
            "status": ride.status.value,
            "pickup_name": ride.pickup_name,
            "dropoff_name": ride.dropoff_name,
            "estimated_fare_ugx": ride.estimated_fare_ugx,
            "estimated_distance_km": ride.estimated_distance_km,
            "estimated_duration_minutes": ride.estimated_duration_minutes,
            "passenger_name": passenger.name if passenger else "Passenger",
            "passenger_phone": passenger.phone_number if passenger else "",
        }
    }


@driver_router.post("/documents")
async def upload_driver_document(
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
    doc_type: str = Form(...),   # national_id | license | registration
    file: UploadFile = File(...),
):
    """
    Driver uploads a verification document (national ID, license, or vehicle
    registration). Re-uploading any document resets is_documents_verified to
    False - a changed document needs a fresh admin review before it counts.
    """
    valid_types = {"national_id", "license", "registration"}
    if doc_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"doc_type must be one of {sorted(valid_types)}")

    allowed_extensions = {".jpg", ".jpeg", ".png", ".pdf"}
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, or PDF files are accepted")

    content = await file.read()
    max_bytes = settings.STORAGE_MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File exceeds {settings.STORAGE_MAX_UPLOAD_MB}MB limit")

    from app.services.storage import storage_service
    key = await storage_service.upload(
        driver_user_id=str(current_user.id),
        doc_type=doc_type,
        filename=file.filename,
        content=content,
    )

    column_map = {
        "national_id": "national_id_doc",
        "license": "license_doc",
        "registration": "registration_doc",
    }

    await db.execute(
        update(DriverProfile)
        .where(DriverProfile.user_id == current_user.id)
        .values(**{column_map[doc_type]: key, "is_documents_verified": False})
    )
    await db.commit()

    return {
        "message": f"{doc_type.replace('_', ' ')} document uploaded. Pending admin verification.",
        "storage_key": key,
    }


# ────────────────────────────────────────────────────────────────────────────────
"""
app/api/routes/payments.py - Wallet and PesaPal endpoints
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_current_passenger, get_current_driver
from app.db.session import get_db
from app.models.models import User
from app.services.payment import payment_service

payments_router = APIRouter(prefix="/payments", tags=["Payments"])


class TopupBody(BaseModel):
    amount_ugx: int = Field(..., ge=1000, le=5000000)


class WithdrawBody(BaseModel):
    amount_ugx: int = Field(..., ge=5000)
    phone_number: str


@payments_router.post("/topup/initiate")
async def initiate_topup(
    body: TopupBody,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """Initiate wallet top-up. Returns PesaPal redirect URL."""
    result = await payment_service.initiate_wallet_topup(
        user=current_user,
        amount_ugx=body.amount_ugx,
        db=db,
    )
    return result


@payments_router.get("/pesapal/ipn")
async def pesapal_ipn(
    OrderTrackingId: str,
    OrderMerchantReference: str,
    OrderNotificationType: str,
    db: AsyncSession = Depends(get_db),
):
    """
    PesaPal IPN callback endpoint.
    PesaPal calls this after payment is completed.
    Must respond with 200 quickly - do heavy processing async.
    """
    success = await payment_service.process_ipn_callback(
        order_tracking_id=OrderTrackingId,
        order_merchant_reference=OrderMerchantReference,
        db=db,
    )
    # PesaPal requires this exact response format
    return {"orderNotificationType": OrderNotificationType, "orderTrackingId": OrderTrackingId, "orderMerchantReference": OrderMerchantReference, "status": 200}


@payments_router.get("/wallet/balance")
async def get_wallet_balance(
    current_user: Annotated[User, Depends(get_current_user)],
):
    return {"balance_ugx": current_user.wallet_balance_ugx}


@payments_router.post("/withdraw")
async def withdraw(
    body: WithdrawBody,
    current_user: Annotated[User, Depends(get_current_driver)],
    db: AsyncSession = Depends(get_db),
):
    """Driver withdraws earnings to mobile money."""
    result = await payment_service.initiate_driver_withdrawal(
        driver=current_user,
        amount_ugx=body.amount_ugx,
        phone_number=body.phone_number,
        db=db,
    )
    return result


@payments_router.get("/transactions")
async def get_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """Transaction history for current user."""
    from sqlalchemy import select
    from app.models.models import Transaction

    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == current_user.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    transactions = result.scalars().all()

    return [
        {
            "id": str(t.id),
            "type": t.type.value,
            "status": t.status.value,
            "amount_ugx": t.amount_ugx,
            "description": t.description,
            "balance_after_ugx": t.balance_after_ugx,
            "created_at": t.created_at.isoformat(),
        }
        for t in transactions
    ]
