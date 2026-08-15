# app/core/celery_app.py
"""Canonical Celery application for all DOERS asynchronous work."""

from celery import Celery, bootsteps
from celery.schedules import crontab

from app.core.config import settings


WORKER_QUEUE = "worker"
# Historical queue label retained for deployment compatibility. The process is
# the isolated maintenance control plane and now hosts both lifecycle and
# narrowly bounded platform-maintenance tasks.
MAINTENANCE_QUEUE = "lifecycle-maintenance"
MAINTENANCE_TASKS = (
    "app.tasks.branch_lifecycle_sweeps.watchdog",
    "app.tasks.branch_lifecycle_sweeps.reconciliation",
    "app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
    "app.tasks.platform_maintenance.advance_trial_lifecycles",
    "app.tasks.platform_maintenance.reclaim_stale_idempotency",
    "app.tasks.platform_maintenance.archive_expired_idempotency",
    "app.tasks.platform_maintenance.geocoding_reverification",
    "app.tasks.platform_maintenance.cleanup_places_cache",
)

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
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_always_eager=(settings.ENVIRONMENT == "development"),
    task_default_queue=WORKER_QUEUE,
    task_routes={
        task_name: {"queue": MAINTENANCE_QUEUE}
        for task_name in MAINTENANCE_TASKS
    },
)


class RuntimeDatabaseIdentityBootstep(bootsteps.StartStopStep):
    """Fail worker startup before broker consumption on DB identity drift."""

    requires = ()

    def start(self, worker) -> None:
        if not settings.is_production:
            return

        profile = settings.celery_worker_profile
        if profile not in {"worker", "maintenance"}:
            raise RuntimeError(
                "Production Celery workers require CELERY_WORKER_PROFILE=worker "
                "or CELERY_WORKER_PROFILE=maintenance"
            )

        from app.core.runtime_principal_attestation import attest_configured_runtime_bindings

        attest_configured_runtime_bindings((profile,))


celery_app.steps["worker"].add(RuntimeDatabaseIdentityBootstep)
celery_app.autodiscover_tasks(["app.tasks"])

celery_app.conf.beat_schedule = {
    "trial-lifecycle-maintenance": {
        "task": "app.tasks.platform_maintenance.advance_trial_lifecycles",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "expire-subscriptions": {
        "task": "app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
        "schedule": crontab(hour=0, minute=5),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    # Legacy reminder, daily-digest and orphan-object sweep schedules are
    # intentionally absent. Their prior implementations performed cross-tenant
    # discovery and/or external side effects under worker_runtime without a
    # durable authorization/idempotency boundary. P3E keeps them fail-closed
    # until they are rebuilt as tenant-bound durable delivery/cleanup workflows.
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
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "reconciliation-sweep": {
        "task": "app.tasks.branch_lifecycle_sweeps.reconciliation",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "platform-idempotency-zombie-reclaim": {
        "task": "app.tasks.platform_maintenance.reclaim_stale_idempotency",
        "schedule": crontab(minute="*"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "platform-idempotency-anchor-archive": {
        "task": "app.tasks.platform_maintenance.archive_expired_idempotency",
        "schedule": crontab(minute=20, hour="*/6"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "platform-geocoding-reverification": {
        "task": "app.tasks.platform_maintenance.geocoding_reverification",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "platform-places-cache-cleanup": {
        "task": "app.tasks.platform_maintenance.cleanup_places_cache",
        "schedule": crontab(hour=4, minute=10),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
}
