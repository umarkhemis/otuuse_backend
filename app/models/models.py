"""
app/models/models.py
--------------------
Full SQLAlchemy ORM models for the Kabale Transport Platform.
Uses PostGIS geometry columns for all geospatial data.

Tables:
    users               - all platform accounts (passengers, drivers, admins)
    driver_profiles     - driver-specific data linked to a user
    otp_records         - OTP tokens for phone verification
    refresh_tokens      - JWT refresh token store for revocation
    rides               - full lifecycle of every ride
    gps_trail_points    - every GPS coordinate recorded during a ride
    deliveries          - delivery requests routed through the admin
    messages            - full conversation history per user
    transactions        - immutable financial transaction log
    driver_strikes      - accountability strike records
    subscriptions       - driver subscription payment records
    push_tokens         - FCM device tokens per user
"""

import uuid
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func

import enum


# ── Base ───────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


def now_utc():
    return datetime.now(timezone.utc)


# ── Enumerations ───────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    PASSENGER = "passenger"
    DRIVER = "driver"
    ADMIN = "admin"


class DriverAvailability(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    ON_RIDE = "on_ride"


class RideStatus(str, enum.Enum):
    REQUESTED = "requested"
    MATCHED = "matched"
    ACCEPTED = "accepted"
    DRIVER_ARRIVING = "driver_arriving"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PAID = "paid"
    CANCELLED = "cancelled"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    NEGOTIATING = "negotiating"
    CONFIRMED = "confirmed"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MessageRole(str, enum.Enum):
    USER = "user"
    AGENT = "agent"
    ADMIN = "admin"


class MessageIntent(str, enum.Enum):
    RIDE_REQUEST = "ride_request"
    DELIVERY_REQUEST = "delivery_request"
    STATUS_INQUIRY = "status_inquiry"
    CANCELLATION = "cancellation"
    GENERAL_QUESTION = "general_question"
    GREETING = "greeting"
    UNCLEAR = "unclear"
    NONE = "none"


class TransactionType(str, enum.Enum):
    WALLET_TOPUP = "wallet_topup"
    RIDE_PAYMENT = "ride_payment"
    COMMISSION = "commission"
    DRIVER_CREDIT = "driver_credit"
    WITHDRAWAL = "withdrawal"
    REFUND = "refund"
    SUBSCRIPTION = "subscription"


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERSED = "reversed"


class StrikeReason(str, enum.Enum):
    NO_SHOW = "no_show"
    OFF_PLATFORM = "off_platform"
    PASSENGER_COMPLAINT = "passenger_complaint"
    GPS_FRAUD = "gps_fraud"
    OTHER = "other"


# ── Users ──────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(20), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    role = Column(Enum(UserRole, values_callable=lambda obj: [e.value for e in obj]), nullable=False, index=True)
    pin_hash = Column(String(200), nullable=True)  # admin-only 2nd factor; null until first admin login sets it
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)

    # Wallet - stored as integer UGX to avoid floating point issues
    wallet_balance_ugx = Column(BigInteger, default=0, nullable=False)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_seen_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("wallet_balance_ugx >= 0", name="ck_wallet_non_negative"),
    )

    # Relationships
    driver_profile = relationship(
        "DriverProfile", back_populates="user", uselist=False,
        foreign_keys="DriverProfile.user_id",
    )
    rides_as_passenger = relationship("Ride", foreign_keys="Ride.passenger_id", back_populates="passenger")
    rides_as_driver = relationship("Ride", foreign_keys="Ride.driver_id", back_populates="driver")
    messages = relationship("Message", back_populates="user")
    transactions = relationship("Transaction", back_populates="user")
    push_tokens = relationship("PushToken", back_populates="user")
    refresh_tokens = relationship("RefreshToken", back_populates="user")
    otp_records = relationship("OTPRecord", back_populates="user")


class DriverProfile(Base):
    __tablename__ = "driver_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)

    # Operational state
    availability = Column(Enum(DriverAvailability, values_callable=lambda obj: [e.value for e in obj]), default=DriverAvailability.OFFLINE, nullable=False, index=True)

    # Current GPS location stored as PostGIS Point (SRID 4326 = WGS84 - standard GPS)
    current_location = Column(Geometry(geometry_type="POINT", srid=4326))
    location_updated_at = Column(DateTime(timezone=True))

    # Performance
    rating = Column(Float, default=5.0, nullable=False)
    total_rides = Column(Integer, default=0, nullable=False)
    total_earnings_ugx = Column(BigInteger, default=0, nullable=False)

    # Subscription
    subscription_active = Column(Boolean, default=False, nullable=False)
    subscription_expires_at = Column(DateTime(timezone=True))

    # Verification documents (stored as S3/DO Spaces keys)
    national_id_doc = Column(String(500))
    license_doc = Column(String(500))
    registration_doc = Column(String(500))
    is_documents_verified = Column(Boolean, default=False, nullable=False)
    plate_number = Column(String(20), nullable=True)   # vehicle plate e.g. UAX 001B

    # PIN for driver login (hashed)
    pin_hash = Column(String(200), nullable=False)

    # Onboarding
    invite_code = Column(String(20), unique=True, index=True)
    onboarded_at = Column(DateTime(timezone=True))
    onboarded_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("rating >= 1.0 AND rating <= 5.0", name="ck_rating_range"),
        # Spatial index for fast proximity queries
        Index("idx_driver_location_gist", "current_location", postgresql_using="gist"),
        Index("idx_driver_availability", "availability"),
    )

    user = relationship("User", back_populates="driver_profile", foreign_keys=[user_id])
    strikes = relationship("DriverStrike", back_populates="driver")
    subscriptions = relationship("Subscription", back_populates="driver")


# ── Auth Records ───────────────────────────────────────────────────────────────

class OTPRecord(Base):
    __tablename__ = "otp_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    otp_hash = Column(String(200), nullable=False)
    purpose = Column(String(50), nullable=False)   # login | register | reset
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_otp_user_purpose", "user_id", "purpose"),
    )

    user = relationship("User", back_populates="otp_records")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(200), nullable=False, unique=True)
    jti = Column(String(64), nullable=False, unique=True)  # JWT ID for revocation
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_refresh_token_jti", "jti"),
        Index("idx_refresh_token_user", "user_id"),
    )

    user = relationship("User", back_populates="refresh_tokens")


# ── Rides ──────────────────────────────────────────────────────────────────────

class Ride(Base):
    __tablename__ = "rides"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    passenger_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)

    # Locations as PostGIS Points
    pickup_location = Column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    dropoff_location = Column(Geometry(geometry_type="POINT", srid=4326), nullable=False)

    # Human-readable location names from geocoding
    pickup_name = Column(String(300), nullable=False)
    dropoff_name = Column(String(300), nullable=False)

    # Routing data
    estimated_distance_km = Column(Float)
    estimated_duration_minutes = Column(Float)
    actual_distance_km = Column(Float)          # calculated from GPS trail on completion
    actual_duration_minutes = Column(Float)

    # Pricing
    estimated_fare_ugx = Column(BigInteger, nullable=False)
    final_fare_ugx = Column(BigInteger)
    commission_ugx = Column(BigInteger)
    driver_earnings_ugx = Column(BigInteger)

    # State
    status = Column(Enum(RideStatus, values_callable=lambda obj: [e.value for e in obj]), default=RideStatus.REQUESTED, nullable=False, index=True)

    # Timestamps for each state transition
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    matched_at = Column(DateTime(timezone=True))
    accepted_at = Column(DateTime(timezone=True))
    driver_arrived_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    paid_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    cancellation_reason = Column(String(300))

    # Ratings
    passenger_rating = Column(Integer)    # rating given by passenger to driver (1-5)
    driver_rating = Column(Integer)       # rating given by driver to passenger (1-5)
    passenger_review = Column(Text)

    # Reassignment tracking
    reassignment_count = Column(Integer, default=0, nullable=False)

    # Link back to the originating message
    originating_message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)

    __table_args__ = (
        CheckConstraint("passenger_rating IS NULL OR (passenger_rating >= 1 AND passenger_rating <= 5)", name="ck_passenger_rating"),
        CheckConstraint("driver_rating IS NULL OR (driver_rating >= 1 AND driver_rating <= 5)", name="ck_driver_rating"),
        Index("idx_ride_status", "status"),
        Index("idx_ride_passenger", "passenger_id"),
        Index("idx_ride_driver", "driver_id"),
        Index("idx_ride_pickup_gist", "pickup_location", postgresql_using="gist"),
    )

    passenger = relationship("User", foreign_keys=[passenger_id], back_populates="rides_as_passenger")
    driver = relationship("User", foreign_keys=[driver_id], back_populates="rides_as_driver")
    gps_trail = relationship("GPSTrailPoint", back_populates="ride", order_by="GPSTrailPoint.recorded_at")
    transactions = relationship("Transaction", back_populates="ride")


class GPSTrailPoint(Base):
    """
    Every GPS coordinate broadcast by the driver during an active ride.
    Used for: distance calculation, dispute resolution, fraud detection.
    This table grows fast - partition by ride_id or by month in production.
    """
    __tablename__ = "gps_trail_points"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ride_id = Column(UUID(as_uuid=True), ForeignKey("rides.id", ondelete="CASCADE"), nullable=False, index=True)
    driver_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # PostGIS Point
    location = Column(Geometry(geometry_type="POINT", srid=4326), nullable=False)

    speed_kmh = Column(Float)
    accuracy_meters = Column(Float)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_gps_trail_ride_time", "ride_id", "recorded_at"),
    )

    ride = relationship("Ride", back_populates="gps_trail")


# ── Deliveries ─────────────────────────────────────────────────────────────────

class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    passenger_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    # Details collected by the agent
    pickup_name = Column(String(300), nullable=False)
    dropoff_name = Column(String(300), nullable=False)
    item_description = Column(Text, nullable=False)
    is_urgent = Column(Boolean, default=False, nullable=False)

    # Agreed pricing (set by admin during negotiation)
    agreed_fare_ugx = Column(BigInteger)

    status = Column(Enum(DeliveryStatus, values_callable=lambda obj: [e.value for e in obj]), default=DeliveryStatus.PENDING, nullable=False, index=True)

    # Admin working notes (internal, not shown to passenger)
    admin_notes = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    resolved_at = Column(DateTime(timezone=True))

    passenger = relationship("User", foreign_keys=[passenger_id])
    messages = relationship("Message", back_populates="delivery")


# ── Messages ───────────────────────────────────────────────────────────────────

class Message(Base):
    """
    Full conversation history. Every message in every conversation is stored here.
    This is the source of truth for agent context and audit purposes.
    """
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(Enum(MessageRole, values_callable=lambda obj: [e.value for e in obj]), nullable=False)   # who sent this message
    content = Column(Text, nullable=False)
    intent = Column(Enum(MessageIntent, values_callable=lambda obj: [e.value for e in obj]), default=MessageIntent.NONE)

    # Optional links to the action this message triggered
    ride_id = Column(UUID(as_uuid=True), ForeignKey("rides.id"), nullable=True)
    delivery_id = Column(UUID(as_uuid=True), ForeignKey("deliveries.id"), nullable=True)

    # Token count for API cost tracking
    token_count = Column(Integer)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("idx_message_user_time", "user_id", "created_at"),
    )

    user = relationship("User", back_populates="messages")
    delivery = relationship("Delivery", back_populates="messages")


# ── Transactions ───────────────────────────────────────────────────────────────

class Transaction(Base):
    """
    Immutable financial ledger. Never update or delete rows here.
    Every money movement - topups, debits, commissions, withdrawals - is a row.
    """
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    type = Column(Enum(TransactionType, values_callable=lambda obj: [e.value for e in obj]), nullable=False)
    status = Column(Enum(TransactionStatus, values_callable=lambda obj: [e.value for e in obj]), default=TransactionStatus.PENDING, nullable=False)

    amount_ugx = Column(BigInteger, nullable=False)

    # Reference to the ride this transaction is for (if applicable)
    ride_id = Column(UUID(as_uuid=True), ForeignKey("rides.id"), nullable=True)

    # PesaPal reference numbers
    pesapal_order_id = Column(String(200), unique=True, nullable=True)
    pesapal_tracking_id = Column(String(200), nullable=True)

    # Human-readable description
    description = Column(String(500))

    # Running wallet balance after this transaction (for statement view)
    balance_after_ugx = Column(BigInteger)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    settled_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("amount_ugx > 0", name="ck_transaction_positive"),
        Index("idx_transaction_user_time", "user_id", "created_at"),
        Index("idx_transaction_pesapal", "pesapal_order_id"),
    )

    user = relationship("User", back_populates="transactions")
    ride = relationship("Ride", back_populates="transactions")


# ── Driver Accountability ──────────────────────────────────────────────────────

class DriverStrike(Base):
    __tablename__ = "driver_strikes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    driver_id = Column(UUID(as_uuid=True), ForeignKey("driver_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    issued_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    ride_id = Column(UUID(as_uuid=True), ForeignKey("rides.id"), nullable=True)

    reason = Column(Enum(StrikeReason, values_callable=lambda obj: [e.value for e in obj]), nullable=False)
    notes = Column(Text)
    is_active = Column(Boolean, default=True, nullable=False)   # can be pardoned by admin

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_strike_driver", "driver_id", "is_active"),
    )

    driver = relationship("DriverProfile", back_populates="strikes")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    driver_id = Column(UUID(as_uuid=True), ForeignKey("driver_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True)

    amount_ugx = Column(BigInteger, nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    is_paid = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    driver = relationship("DriverProfile", back_populates="subscriptions")


# ── Push Tokens ────────────────────────────────────────────────────────────────

class PushToken(Base):
    __tablename__ = "push_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    fcm_token = Column(String(500), nullable=False)
    device_type = Column(String(20))   # android | ios
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "fcm_token", name="uq_user_fcm_token"),
    )

    user = relationship("User", back_populates="push_tokens")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action = Column(String(100), nullable=False)
    target_type = Column(String(50))
    target_id = Column(String(100))
    details = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
