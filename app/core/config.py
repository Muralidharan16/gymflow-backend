import os
from typing import List
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Database ───────────────────────────────────
    DATABASE_URL: str
    AUTH_DATABASE_URL: str = ""
    WORKER_DATABASE_URL: str = ""
    MAINTENANCE_DATABASE_URL: str = ""
    TEST_DATABASE_URL: str = ""
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
    MAIL_PROVIDER: str = "smtp"
    MAIL_FROM: str = "noreply@doers.io"
    MAIL_SERVER: str = "sandbox.smtp.mailtrap.io"
    MAIL_PORT: int = 587
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
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

    # ── Platform Billing Feature Flags ─────────────
    PLATFORM_BILLING_READ_API: bool = False
    PLATFORM_BILLING_SHADOW_RESOLVER: bool = False
    PLATFORM_BILLING_ENFORCEMENT: bool = False
    PLATFORM_BILLING_FRONTEND_SHELL: bool = False
    PLATFORM_BILLING_CHECKOUT: bool = False
    PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED: bool = False
    PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED: bool = False
    PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED: bool = False
    PLATFORM_BILLING_PROVIDER_MODE: str = "disabled"
    PLATFORM_BILLING_WEBHOOK_PAYLOAD_STORE_DIR: str = "/tmp/gymflow-platform-webhook-payloads"
    PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR: str = "/tmp/gymflow-platform-fake-provider-evidence"
    PLATFORM_BILLING_WEBHOOK_PROCESSING: bool = False
    PLATFORM_BILLING_DUNNING_TRANSITIONS: bool = False
    PLATFORM_BILLING_NOTIFICATIONS: bool = False

    # ── Storage (S3) ───────────────────────────────
    AWS_ACCESS_KEY_ID: str = Field(..., alias="AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str = Field(..., alias="AWS_SECRET_ACCESS_KEY")
    AWS_REGION_NAME: str = "us-east-1"
    S3_BUCKET_NAME: str = "gymflow-local-bucket"
    CDN_BASE_URL: str = "https://cdn.gymflow.local"

    @model_validator(mode="after")
    def validate_database_identity_boundaries(self):
        if self.ENVIRONMENT == "production":
            if not self.AUTH_DATABASE_URL:
                raise ValueError(
                    "AUTH_DATABASE_URL is required in production so auth/bootstrap "
                    "does not share the ordinary application database identity"
                )
            if not self.WORKER_DATABASE_URL:
                raise ValueError(
                    "WORKER_DATABASE_URL is required in production so asynchronous "
                    "workers do not reuse API or auth credentials"
                )
            if not self.MAINTENANCE_DATABASE_URL:
                raise ValueError(
                    "MAINTENANCE_DATABASE_URL is required in production so lifecycle "
                    "watchdog/reconciliation sweeps do not reuse API, auth, or worker credentials"
                )

            from app.core.runtime_principal_attestation import validate_runtime_url_configuration

            violations = validate_runtime_url_configuration({
                "api": self.DATABASE_URL,
                "auth": self.AUTH_DATABASE_URL,
                "worker": self.WORKER_DATABASE_URL,
                "maintenance": self.MAINTENANCE_DATABASE_URL,
            })
            if violations:
                detail = "; ".join(
                    f"[{item.code}] {item.subject}: {item.message}"
                    for item in violations
                )
                raise ValueError(
                    "Production database identity configuration is unsafe: " + detail
                )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def worker_database_url(self) -> str:
        """Return the bounded worker URL.

        Production is fail-closed by the model validator above. Development and
        test environments may intentionally share the application URL so the
        repository remains easy to run locally before a dedicated worker login
        is provisioned.
        """
        return self.WORKER_DATABASE_URL or self.DATABASE_URL

    @property
    def maintenance_database_url(self) -> str:
        """Return the bounded lifecycle-maintenance URL.

        Production must use a fourth database identity. Development and tests
        may share the application URL until the dedicated maintenance login is
        provisioned, matching the worker-local-development behavior above.
        """
        return self.MAINTENANCE_DATABASE_URL or self.DATABASE_URL

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


settings = Settings()
