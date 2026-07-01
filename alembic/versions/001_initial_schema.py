"""
alembic/versions/001_initial_schema.py
---------------------------------------
Initial database migration.
Creates all tables and indexes.
Requires the PostGIS extension to be enabled in PostgreSQL.
Run: alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import geoalchemy2

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable PostGIS extension (run once per database)
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis_topology")

    # ── users ──────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_number", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.Enum("passenger", "driver", "admin", name="userrole"), nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("is_verified", sa.Boolean, default=False, nullable=False),
        sa.Column("wallet_balance_ugx", sa.BigInteger, default=0, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("wallet_balance_ugx >= 0", name="ck_wallet_non_negative"),
    )
    op.create_index("idx_users_phone", "users", ["phone_number"])
    op.create_index("idx_users_role", "users", ["role"])

    # ── driver_profiles ────────────────────────────────────────────────────────
    op.create_table(
        "driver_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("availability", sa.Enum("online", "offline", "on_ride", name="driveravailability"), default="offline", nullable=False),
        sa.Column("current_location", geoalchemy2.Geometry(geometry_type="POINT", srid=4326), nullable=True),
        sa.Column("location_updated_at", sa.DateTime(timezone=True)),
        sa.Column("rating", sa.Float, default=5.0, nullable=False),
        sa.Column("total_rides", sa.Integer, default=0, nullable=False),
        sa.Column("total_earnings_ugx", sa.BigInteger, default=0, nullable=False),
        sa.Column("subscription_active", sa.Boolean, default=False, nullable=False),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True)),
        sa.Column("national_id_doc", sa.String(500)),
        sa.Column("license_doc", sa.String(500)),
        sa.Column("registration_doc", sa.String(500)),
        sa.Column("is_documents_verified", sa.Boolean, default=False, nullable=False),
        sa.Column("pin_hash", sa.String(200), nullable=False),
        sa.Column("invite_code", sa.String(20), unique=True),
        sa.Column("onboarded_at", sa.DateTime(timezone=True)),
        sa.Column("onboarded_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.CheckConstraint("rating >= 1.0 AND rating <= 5.0", name="ck_rating_range"),
    )
    op.execute("CREATE INDEX idx_driver_location_gist ON driver_profiles USING GIST (current_location)")
    op.create_index("idx_driver_availability", "driver_profiles", ["availability"])

    # ── otp_records ────────────────────────────────────────────────────────────
    op.create_table(
        "otp_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("otp_hash", sa.String(200), nullable=False),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_otp_user_purpose", "otp_records", ["user_id", "purpose"])

    # ── refresh_tokens ─────────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(200), nullable=False, unique=True),
        sa.Column("jti", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_refresh_token_jti", "refresh_tokens", ["jti"])
    op.create_index("idx_refresh_token_user", "refresh_tokens", ["user_id"])

    # ── rides ──────────────────────────────────────────────────────────────────
    op.create_table(
        "rides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("passenger_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("pickup_location", geoalchemy2.Geometry(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("dropoff_location", geoalchemy2.Geometry(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("pickup_name", sa.String(300), nullable=False),
        sa.Column("dropoff_name", sa.String(300), nullable=False),
        sa.Column("estimated_distance_km", sa.Float),
        sa.Column("estimated_duration_minutes", sa.Float),
        sa.Column("actual_distance_km", sa.Float),
        sa.Column("actual_duration_minutes", sa.Float),
        sa.Column("estimated_fare_ugx", sa.BigInteger, nullable=False),
        sa.Column("final_fare_ugx", sa.BigInteger),
        sa.Column("commission_ugx", sa.BigInteger),
        sa.Column("driver_earnings_ugx", sa.BigInteger),
        sa.Column("status", sa.Enum("requested", "matched", "accepted", "driver_arriving", "in_progress", "completed", "paid", "cancelled", name="ridestatus"), default="requested", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("driver_arrived_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(300)),
        sa.Column("passenger_rating", sa.Integer),
        sa.Column("driver_rating", sa.Integer),
        sa.Column("passenger_review", sa.Text),
        sa.Column("reassignment_count", sa.Integer, default=0, nullable=False),
        sa.Column("originating_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("passenger_rating IS NULL OR (passenger_rating >= 1 AND passenger_rating <= 5)", name="ck_passenger_rating"),
    )
    op.create_index("idx_ride_status", "rides", ["status"])
    op.create_index("idx_ride_passenger", "rides", ["passenger_id"])
    op.create_index("idx_ride_driver", "rides", ["driver_id"])
    op.execute("CREATE INDEX idx_ride_pickup_gist ON rides USING GIST (pickup_location)")

    # ── gps_trail_points ───────────────────────────────────────────────────────
    op.create_table(
        "gps_trail_points",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rides.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("location", geoalchemy2.Geometry(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("speed_kmh", sa.Float),
        sa.Column("accuracy_meters", sa.Float),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_gps_trail_ride_time", "gps_trail_points", ["ride_id", "recorded_at"])

    # ── deliveries ─────────────────────────────────────────────────────────────
    op.create_table(
        "deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("passenger_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("pickup_name", sa.String(300), nullable=False),
        sa.Column("dropoff_name", sa.String(300), nullable=False),
        sa.Column("item_description", sa.Text, nullable=False),
        sa.Column("is_urgent", sa.Boolean, default=False, nullable=False),
        sa.Column("agreed_fare_ugx", sa.BigInteger),
        sa.Column("status", sa.Enum("pending", "under_review", "negotiating", "confirmed", "assigned", "in_progress", "completed", "cancelled", name="deliverystatus"), default="pending", nullable=False),
        sa.Column("admin_notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )

    # ── messages ───────────────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Enum("user", "agent", "admin", name="messagerole"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("intent", sa.Enum("ride_request", "delivery_request", "status_inquiry", "cancellation", "general_question", "greeting", "unclear", "none", name="messageintent"), default="none"),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rides.id"), nullable=True),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deliveries.id"), nullable=True),
        sa.Column("token_count", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_message_user_time", "messages", ["user_id", "created_at"])

    # ── transactions ───────────────────────────────────────────────────────────
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.Enum("wallet_topup", "ride_payment", "commission", "driver_credit", "withdrawal", "refund", "subscription", name="transactiontype"), nullable=False),
        sa.Column("status", sa.Enum("pending", "completed", "failed", "reversed", name="transactionstatus"), default="pending", nullable=False),
        sa.Column("amount_ugx", sa.BigInteger, nullable=False),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rides.id"), nullable=True),
        sa.Column("pesapal_order_id", sa.String(200), unique=True, nullable=True),
        sa.Column("pesapal_tracking_id", sa.String(200), nullable=True),
        sa.Column("description", sa.String(500)),
        sa.Column("balance_after_ugx", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_ugx > 0", name="ck_transaction_positive"),
    )
    op.create_index("idx_transaction_user_time", "transactions", ["user_id", "created_at"])
    op.create_index("idx_transaction_pesapal", "transactions", ["pesapal_order_id"])

    # ── driver_strikes ─────────────────────────────────────────────────────────
    op.create_table(
        "driver_strikes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("driver_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issued_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rides.id"), nullable=True),
        sa.Column("reason", sa.Enum("no_show", "off_platform", "passenger_complaint", "gps_fraud", "other", name="strikereason"), nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_strike_driver", "driver_strikes", ["driver_id", "is_active"])

    # ── subscriptions ──────────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("driver_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("amount_ugx", sa.BigInteger, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_paid", sa.Boolean, default=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── push_tokens ────────────────────────────────────────────────────────────
    op.create_table(
        "push_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fcm_token", sa.String(500), nullable=False),
        sa.Column("device_type", sa.String(20)),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.UniqueConstraint("user_id", "fcm_token", name="uq_user_fcm_token"),
    )


def downgrade() -> None:
    op.drop_table("push_tokens")
    op.drop_table("subscriptions")
    op.drop_table("driver_strikes")
    op.drop_table("transactions")
    op.drop_table("messages")
    op.drop_table("deliveries")
    op.drop_table("gps_trail_points")
    op.drop_table("rides")
    op.drop_table("refresh_tokens")
    op.drop_table("otp_records")
    op.drop_table("driver_profiles")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS userrole")
    op.execute("DROP TYPE IF EXISTS driveravailability")
    op.execute("DROP TYPE IF EXISTS ridestatus")
    op.execute("DROP TYPE IF EXISTS deliverystatus")
    op.execute("DROP TYPE IF EXISTS messagerole")
    op.execute("DROP TYPE IF EXISTS messageintent")
    op.execute("DROP TYPE IF EXISTS transactiontype")
    op.execute("DROP TYPE IF EXISTS transactionstatus")
    op.execute("DROP TYPE IF EXISTS strikereason")
