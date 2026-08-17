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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    BACKEND_BASE_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:3000"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174"

    # Legacy/authentication mail path. P4C lifecycle delivery intentionally uses
    # its own worker credential below so onboarding mail and member effects do
    # not share a deployment secret by accident.
    MAIL_PROVIDER: str = "smtp"
    MAIL_FROM: str = "noreply@doers.io"
    MAIL_SERVER: str = "sandbox.smtp.mailtrap.io"
    MAIL_PORT: int = 587
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    RESEND_API_KEY: str = ""

    # P4C durable member notifications. Sending authority belongs to the
    # ordinary worker; webhook verification belongs to the API boundary.
    NOTIFICATION_EMAIL_PROVIDER_MODE: str = "disabled"
    P4C_RESEND_API_KEY: str = ""
    NOTIFICATION_EMAIL_FROM: str = "members@doers.io"
    RESEND_API_BASE_URL: str = "https://api.resend.com"
    NOTIFICATION_PROVIDER_TIMEOUT_SECONDS: float = 8.0
    RESEND_WEBHOOK_SECRET: str = ""
    NOTIFICATION_METRICS_OTLP_ENDPOINT: str = ""
    NOTIFICATION_METRICS_EXPORT_INTERVAL_SECONDS: float = 30.0
    NOTIFICATION_METRICS_EXPORT_TIMEOUT_SECONDS: float = 5.0

    WA_PHONE_NUMBER_ID: str = ""
    WA_ACCESS_TOKEN: str = ""
    WA_TEMPLATE_REMINDER: str = "member_renewal_reminder"
    WA_TEMPLATE_DIGEST: str = "daily_digest_notification"
    GOOGLE_MAPS_SERVER_API_KEY: str = ""

    # P4B search integration. The worker fails closed unless mode=opensearch and
    # a concrete endpoint/index are supplied. Defaults intentionally do not make
    # a development process perform external writes by accident.
    SEARCH_PROVIDER_MODE: str = "disabled"
    OPENSEARCH_URL: str = ""
    OPENSEARCH_INDEX: str = "branches-v1"
    OPENSEARCH_USERNAME: str = ""
    OPENSEARCH_PASSWORD: str = ""
    OPENSEARCH_TIMEOUT_SECONDS: float = 5.0
    OPENSEARCH_VERIFY_TLS: bool = True
    SEARCH_METRICS_OTLP_ENDPOINT: str = ""
    SEARCH_METRICS_EXPORT_INTERVAL_SECONDS: float = 30.0
    SEARCH_METRICS_EXPORT_TIMEOUT_SECONDS: float = 5.0

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
    AWS_KMS_KEY_ID: str = ""
    AWS_KMS_ENDPOINT_URL: str = ""
    S3_BUCKET_NAME: str = "gymflow-local-bucket"
    CDN_BASE_URL: str = "https://cdn.gymflow.local"