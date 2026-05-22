import os
from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Database ───────────────────────────────────
    DATABASE_URL: str
    REDIS_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # ── Auth ───────────────────────────────────────
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── URLs ───────────────────────────────────────
    BACKEND_BASE_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:3000"

    # ── CORS ───────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174"

    # ── Email ──────────────────────────────────────
    MAIL_PROVIDER: str = "smtp"          # "smtp" | "resend"
    MAIL_FROM: str = "noreply@doers.io"

    # SMTP (Mailtrap for dev)
    MAIL_SERVER: str = "sandbox.smtp.mailtrap.io"
    MAIL_PORT: int = 587
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""

    # Resend (production)
    RESEND_API_KEY: str = ""

    # ── WhatsApp ───────────────────────────────────
    WA_PHONE_NUMBER_ID: str = ""
    WA_ACCESS_TOKEN: str = ""
    WA_TEMPLATE_REMINDER: str = "member_renewal_reminder"
    WA_TEMPLATE_DIGEST: str = "daily_digest_notification"

    # ── Google Maps ────────────────────────────────
    GOOGLE_MAPS_SERVER_API_KEY: str = ""

    # ── App ────────────────────────────────────────
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "info"

    # ── Storage (S3) ───────────────────────────────
    AWS_ACCESS_KEY_ID: str = Field(..., alias="AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str = Field(..., alias="AWS_SECRET_ACCESS_KEY")
    AWS_REGION_NAME: str = "us-east-1"
    S3_BUCKET_NAME: str = "gymflow-local-bucket"
    CDN_BASE_URL: str = "https://cdn.gymflow.local"

    # ── Properties ─────────────────────────────────
    @property
    def cors_origins_list(self) -> list[str]:
        """Convert comma-separated string to a list of origins."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


settings = Settings()