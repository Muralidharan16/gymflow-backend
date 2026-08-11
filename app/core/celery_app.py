# app/core/celery_app.py
from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "gymflow_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=False,
    # Ensure tasks are acknowledged only after completion
    task_acks_late=True,
    # Limit concurrency for DB-heavy tasks
    worker_prefetch_multiplier=1,
    # Enable eager execution in development for synchronous inline Celery processing
    task_always_eager=(settings.ENVIRONMENT == "development")
)

# Automatic Task Discovery
celery_app.autodiscover_tasks(["app.tasks"])

# Periodic operational schedule (IST).
celery_app.conf.beat_schedule = {
    "daily-trial-monitor": {
        "task": "app.tasks.trial_tasks.monitor_trial_lifecycles",
        "schedule": crontab(hour=0, minute=0),
    },
    # Creation is owned by pg_partman/database infrastructure.  The application
    # worker performs a read-only coverage check so partition-maintenance drift
    # becomes an observable task failure without granting runtime DDL rights.
    "daily-branch-hours-audit-partition-readiness": {
        "task": "app.tasks.branch_hours_partition.run",
        "schedule": crontab(hour=3, minute=15),
    },
}
