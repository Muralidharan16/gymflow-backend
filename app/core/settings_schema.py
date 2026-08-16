from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DoersSettingsSchema(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    API_DATABASE_INPUT: str = Field("", alias="DATABASE_URL")
    AUTH_DATABASE_INPUT: str = Field("", alias="AUTH_DATABASE_URL")
    WORKER_DATABASE_INPUT: str = Field("", alias="WORKER_DATABASE_URL")
    MAINTENANCE_DATABASE_INPUT: str = Field("", alias="MAINTENANCE_DATABASE_URL")
    TEST_DATABASE_URL: str = ""
    REDIS_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    DOERS_PROCESS_PROFILE: str = ""
    CELERY_WORKER_PROFILE: str = ""

    SECRET_KEY: str
    INTERNAL_CONTROL_TOKEN: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    BACKEND_BASE_URL: str = "http://localhost:8000"
    PUBLIC_API_PATH_PREFIX: str = ""
    FRONTEND_URL: str = "http://localhost:3000"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174"

    MAIL_PROVIDER: str = "smtp"
    MAIL_FROM: str = "noreply@doers.io"
    MAIL_SERVER: str = "sandbox.smtp.mailtrap.io"
    MAIL_PORT: int = 587
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    RESEND_API_KEY: str = ""

    WA_PHONE_NUMBER_ID: str = ""
    WA_ACCESS_TOKEN: str = ""
    WA_TEMPLATE_REMINDER: str = "member_renewal_reminder"
    WA_TEMPLATE_DIGEST: str = "daily_digest_notification"
    GOOGLE_MAPS_SERVER_API_KEY: str = ""

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "info"

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

    AWS_ACCESS_KEY_ID: str = Field(..., alias="AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str = Field(..., alias="AWS_SECRET_ACCESS_KEY")
    AWS_REGION_NAME: str = "us-east-1"
    S3_BUCKET_NAME: str = "gymflow-local-bucket"
    CDN_BASE_URL: str = "https://cdn.gymflow.local"
