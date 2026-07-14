"""
app/core/config.py
------------------
Centralised settings loaded from environment variables via pydantic-settings.
All configuration lives here - nothing is hard-coded anywhere else in the app.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    APP_NAME: str = "Kabale Transport Platform"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_DEBUG: bool = False
    APP_SECRET_KEY: str
    APP_BASE_URL: str = "http://localhost:8000"

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10
    DATABASE_POOL_TIMEOUT: int = 30

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_DRIVER_LOCATION_TTL: int = 30
    REDIS_SESSION_TTL: int = 86400

    # ── JWT ────────────────────────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── AI Agent ───────────────────────────────────────────────────────────────
    LLM_PROVIDER: Literal["anthropic", "openai", "groq"] = "groq"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.1-8b-instant"
    LLM_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_MAX_TOKENS: int = 1000
    LLM_CONVERSATION_WINDOW: int = 20

    # ── Geocoding & Routing ────────────────────────────────────────────────────
    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"
    NOMINATIM_USER_AGENT: str = "KabaleTransportPlatform/1.0"
    ORS_BASE_URL: str = "https://api.openrouteservice.org/v2"
    ORS_API_KEY: str = ""

    # ── Pricing ────────────────────────────────────────────────────────────────
    PRICING_BASE_FEE_UGX: int = 1000
    PRICING_RATE_PER_KM_UGX: int = 1200
    PRICING_RATE_PER_MINUTE_UGX: int = 50
    PRICING_MINIMUM_FARE_UGX: int = 2000
    PRICING_COMMISSION_PERCENT: float = 15.0

    # ── File Storage (driver verification documents) ─────────────────────────────
    STORAGE_PROVIDER: Literal["local", "s3"] = "local"
    STORAGE_LOCAL_PATH: str = "uploads"
    STORAGE_S3_ENDPOINT_URL: str = ""    # e.g. https://kabale.fra1.digitaloceanspaces.com
    STORAGE_S3_BUCKET: str = ""
    STORAGE_S3_ACCESS_KEY: str = ""
    STORAGE_S3_SECRET_KEY: str = ""
    STORAGE_S3_REGION: str = "fra1"
    STORAGE_MAX_UPLOAD_MB: int = 8

    # ── Firebase ───────────────────────────────────────────────────────────────
    FIREBASE_CREDENTIALS_PATH: str = "firebase-service-account.json"

    # ── PesaPal ────────────────────────────────────────────────────────────────
    PESAPAL_CONSUMER_KEY: str = ""
    PESAPAL_CONSUMER_SECRET: str = ""
    PESAPAL_BASE_URL: str = "https://cybqa.pesapal.com/pesapalv3"
    PESAPAL_IPN_URL: str = ""
    PESAPAL_CALLBACK_URL: str = ""

    # ── SMS ────────────────────────────────────────────────────────────────────
    SMS_PROVIDER: str = "africastalking"
    AFRICASTALKING_USERNAME: str = ""
    AFRICASTALKING_API_KEY: str = ""
    AFRICASTALKING_SENDER_ID: str = "KabaleTrans"

    # ── Dispatch ───────────────────────────────────────────────────────────────
    DISPATCH_MAX_DRIVER_SEARCH_RADIUS_KM: float = 5.0
    DISPATCH_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS: int = 30
    DISPATCH_MAX_REASSIGNMENT_ATTEMPTS: int = 3
    DISPATCH_ARRIVAL_GEOFENCE_RADIUS_METERS: float = 100.0
    DISPATCH_COMPLETION_GEOFENCE_RADIUS_METERS: float = 150.0
    DISPATCH_AUTO_START_DELAY_SECONDS: int = 180
    DISPATCH_AUTO_COMPLETE_STATIONARY_SECONDS: int = 120

    # Operating hours (East Africa Time = UTC+3)
    OPERATION_START_HOUR: int = 6    # 6am EAT
    OPERATION_END_HOUR: int = 22     # 10pm EAT

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_CHAT_PER_MINUTE: int = 30
    RATE_LIMIT_AUTH_PER_MINUTE: int = 5

    # ── Sentry ─────────────────────────────────────────────────────────────────
    SENTRY_DSN: str = ""

    @field_validator("APP_SECRET_KEY")
    @classmethod
    def secret_key_must_be_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("APP_SECRET_KEY must be at least 32 characters long.")
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache()
def get_settings() -> Settings:
    """
    Returns a cached Settings instance.
    Use this throughout the app instead of instantiating Settings directly.
    The @lru_cache ensures settings are only read from the environment once.
    """
    return Settings()


settings = get_settings()
