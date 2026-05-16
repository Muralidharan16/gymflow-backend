# FIXED: [FIX 4] Added CORS_ORIGINS setting with model_validator for comma-separated .env parsing.
import os
from typing import List
from pydantic import model_validator
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
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

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

    # ── App ────────────────────────────────────────
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "info"

    @model_validator(mode="before")
    @classmethod
    def parse_cors_origins(cls, values: dict) -> dict:
        """Parse CORS_ORIGINS from a comma-separated string in .env."""
        raw = values.get("CORS_ORIGINS")
        if isinstance(raw, str):
            values["CORS_ORIGINS"] = [
                origin.strip() for origin in raw.split(",") if origin.strip()
            ]
        return values

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


settings = Settings()