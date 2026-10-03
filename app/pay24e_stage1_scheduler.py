"""PAY-24-E Stage-1 entitlement scheduler.

This Celery app publishes only the entitlement dispatcher wake-up. It has no
database credential and no provider credential. PostgreSQL PAY-24-E remains the
business authority; Stage 0 and rollback-closing claims return no work.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab


_FORBIDDEN = (
    "DATABASE_URL",
    "AUTH_DATABASE_URL",
    "WORKER_DATABASE_URL",
    "MAINTENANCE_DATABASE_URL",
    "FINANCE_PAYMENT_DATABASE_URL",
    "FINANCE_CONFIG_DATABASE_URL",
    "ENTITLEMENT_DATABASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "P4C_RESEND_API_KEY",
    "RESEND_WEBHOOK_SECRET",
    "WA_ACCESS_TOKEN",
    "GOOGLE_MAPS_SERVER_API_KEY",
    "OPENSEARCH_USERNAME",
    "OPENSEARCH_PASSWORD",
)


def _required(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise RuntimeError(f"PAY-24-E scheduler requires {name}")
    return value


def _validate_environment() -> None:
    leaked = sorted(name for name in _FORBIDDEN if str(os.environ.get(name, "") or "").strip())
    if leaked:
        raise RuntimeError(
            "PAY-24-E scheduler received forbidden credentials: " + ", ".join(leaked)
        )
    if str(os.environ.get("ENVIRONMENT", "")).strip().lower() == "production":
        if str(os.environ.get("PAY24E_STAGE1_SCHEDULER", "")).strip() != "prepared":
            raise RuntimeError(
                "PAY-24-E production scheduler requires PAY24E_STAGE1_SCHEDULER=prepared"
            )


_validate_environment()

celery_app = Celery(
    "doers-pay24e-stage1-entitlement-scheduler",
    broker=_required("CELERY_BROKER_URL"),
    backend=_required("CELERY_RESULT_BACKEND"),
)
celery_app.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_publish_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=None,
    beat_schedule={
        "pay24e-stage1-entitlement-poll": {
            "task": "app.tasks.entitlement_dispatcher.run",
            "schedule": crontab(minute="*"),
            "options": {"queue": "entitlement"},
        }
    },
)
