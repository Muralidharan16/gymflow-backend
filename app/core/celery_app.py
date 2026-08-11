# app/core/celery_app.py
"""Canonical Celery application for all DOERS asynchronous work.

Every worker, beat process, task module, and API-side task producer must bind to
this application. Keeping a single app prevents task-registration and schedule
drift between deployment entrypoints.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings


celery_app = Celery(
    "doers",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    # Keep broker/task timestamps normalized while allowing beat schedules to
    # remain expressed in the configured application timezone.
    enable_utc=True,
    task_track_started=True,
    # A worker crash must not acknowledge unfinished work.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Avoid reserving a large batch of DB-heavy tasks on one worker process.
    worker_prefetch_multiplier=1,
    # Development may intentionally execute tasks inline; production never may.
    task_always_eager=(settings.ENVIRONMENT == "development"),
)

# Automatic Task Discovery
celery_app.autodiscover_tasks(["app.tasks"])

# One authoritative periodic schedule.  The deployment entrypoint imports this
# same Celery object, so workers and beat cannot silently diverge.
celery_app.conf.beat_schedule = {
    "daily-trial-monitor": {
        "task": "app.tasks.trial_tasks.monitor_trial_lifecycles",
        "schedule": crontab(hour=0, minute=0),
    },
    "expire-subscriptions": {
        "task": "app.tasks.expire_subs.run",
        "schedule": crontab(hour=0, minute=5),
    },
    "member-reminders": {
        "task": "app.tasks.reminders.run",
        "schedule": crontab(hour=10, minute=0),
    },
    "daily-digest": {
        "task": "app.tasks.daily_digest.run",
        "schedule": crontab(hour=8, minute=30),
    },
    "orphan-logo-cleanup": {
        "task": "app.tasks.logos.cleanup_orphaned_logos",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # Partition creation is infrastructure-owned. The application performs
    # only a read-only coverage check and fails observably if readiness drifts.
    "daily-branch-hours-audit-partition-readiness": {
        "task": "app.tasks.branch_hours_partition.run",
        "schedule": crontab(hour=3, minute=15),
    },
    "poll-outbox": {
        "task": "app.tasks.outbox_poller.run",
        "schedule": crontab(minute="*"),
    },
    "poll-branch-outbox": {
        "task": "app.tasks.branch_outbox_poller.run",
        "schedule": crontab(minute="*"),
    },
    "watchdog-sweep": {
        "task": "app.tasks.branch_lifecycle_sweeps.watchdog",
        "schedule": crontab(minute="*/5"),
    },
    "reconciliation-sweep": {
        "task": "app.tasks.branch_lifecycle_sweeps.reconciliation",
        "schedule": crontab(minute="*/15"),
    },
}
