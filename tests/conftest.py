"""
tests/conftest.py
-----------------
Shared pytest fixtures for all tests.
Uses an in-memory SQLite database for unit tests
and a real PostgreSQL test database for integration tests.
"""

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.models.models import Base
from app.db.session import get_db
from app.core.security import create_access_token
from app.models.models import UserRole

# ── Test Database ──────────────────────────────────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_db():
    """
    Create a fresh in-memory database for each test.
    All tables are created and dropped per test to ensure isolation.

    The models use PostGIS Geometry columns (via geoalchemy2). SQLite has no
    native geometry support, so SpatiaLite is loaded as a runtime extension
    on every new connection - this gives the in-memory test DB enough spatial
    function support (RecoverGeometryColumn, etc.) for table creation to
    succeed, without needing a real PostgreSQL instance for unit/integration
    tests that don't exercise actual spatial queries.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _load_spatialite(dbapi_conn, conn_record):
        # aiosqlite wraps the real connection; enable_load_extension/load_extension
        # only exist on the underlying driver_connection, and must be reached via
        # run_async since dbapi_conn itself is the async-adapted wrapper.
        dbapi_conn.run_async(lambda conn: conn.enable_load_extension(True))
        dbapi_conn.run_async(lambda conn: conn.load_extension("mod_spatialite"))
        dbapi_conn.run_async(lambda conn: conn.enable_load_extension(False))

    async with engine.begin() as conn:
        await conn.execute(text("SELECT InitSpatialMetaData(1)"))
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(test_db: AsyncSession):
    """
    HTTP test client with database dependency overridden.
    """
    async def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# ── Test User Factories ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def passenger_user(test_db: AsyncSession):
    """Create and return a test passenger user."""
    import uuid
    from app.models.models import User

    user = User(
        id=uuid.uuid4(),
        phone_number="+256700000001",
        name="Test Passenger",
        role=UserRole.PASSENGER,
        is_active=True,
        is_verified=True,
        wallet_balance_ugx=50000,
    )
    test_db.add(user)
    await test_db.commit()
    return user


@pytest_asyncio.fixture
async def driver_user(test_db: AsyncSession):
    """Create and return a test driver user with a profile."""
    import uuid
    from app.models.models import User, DriverProfile, DriverAvailability
    from datetime import datetime, timezone, timedelta
    from app.core.security import hash_pin

    user = User(
        id=uuid.uuid4(),
        phone_number="+256700000002",
        name="Test Driver",
        role=UserRole.DRIVER,
        is_active=True,
        is_verified=True,
        wallet_balance_ugx=0,
    )
    test_db.add(user)
    await test_db.flush()

    profile = DriverProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        availability=DriverAvailability.ONLINE,
        current_location="SRID=4326;POINT(29.9847 -1.2492)",
        rating=4.8,
        total_rides=120,
        subscription_active=True,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        pin_hash=hash_pin("1234"),
    )
    test_db.add(profile)
    await test_db.commit()

    return user, profile


@pytest_asyncio.fixture
async def admin_user(test_db: AsyncSession):
    """Create and return a test admin user."""
    import uuid
    from app.models.models import User

    user = User(
        id=uuid.uuid4(),
        phone_number="+256700000003",
        name="Test Admin",
        role=UserRole.ADMIN,
        is_active=True,
        is_verified=True,
        wallet_balance_ugx=0,
    )
    test_db.add(user)
    await test_db.commit()
    return user


# ── Auth Token Helpers ─────────────────────────────────────────────────────────

def passenger_token(user_id: str) -> str:
    return create_access_token(subject=user_id, role="passenger")


def driver_token(user_id: str) -> str:
    return create_access_token(subject=user_id, role="driver")


def admin_token(user_id: str) -> str:
    return create_access_token(subject=user_id, role="admin")
