"""
app/main.py
-----------
FastAPI application entry point.
- Configures middleware (CORS, rate limiting, security headers)
- Registers all routers
- Handles startup and shutdown lifecycle events
- Configures Sentry for error tracking in production
"""

import time
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.services.cache import close_redis
from app.services.notifications import init_firebase

configure_logging()
logger = get_logger(__name__)


# ── Sentry (Production Error Tracking) ────────────────────────────────────────
if settings.SENTRY_DSN and settings.is_production:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        traces_sample_rate=0.2,
    )


# ── Rate Limiter ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("app_starting", env=settings.APP_ENV)

    # Initialize Firebase for push notifications
    init_firebase()

    # Register PesaPal IPN URL (idempotent)
    if settings.PESAPAL_CONSUMER_KEY:
        try:
            from app.services.payment import payment_service
            ipn_id = await payment_service.register_ipn()
            logger.info("pesapal_ipn_ready", ipn_id=ipn_id)
        except Exception as e:
            logger.warning("pesapal_ipn_registration_failed", error=str(e))

    logger.info("app_started")
    yield

    # Shutdown
    logger.info("app_shutting_down")
    await close_redis()
    logger.info("app_stopped")


# ── Application ────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Middleware ─────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_development else [settings.APP_BASE_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Log all requests with timing."""
    start = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000, 2)

    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


# ── Global Exception Handler ───────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred. Please try again."},
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
from app.api.routes.auth import router as auth_router
from app.api.routes.routes import router as chat_router
from app.api.routes.routes import driver_router
from app.api.routes.routes import payments_router
from app.api.routes.admin import router as admin_router
from app.api.routes.rides import router as rides_router

API_PREFIX = "/api/v1"

app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(chat_router, prefix=API_PREFIX)
app.include_router(driver_router, prefix=API_PREFIX)
app.include_router(payments_router, prefix=API_PREFIX)
app.include_router(admin_router, prefix=API_PREFIX)
app.include_router(rides_router, prefix=API_PREFIX)


# ── Health Check ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/")
async def root():
    return {"message": settings.APP_NAME, "version": "1.0.0"}
