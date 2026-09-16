# app/core/celery_app.py
"""Canonical Celery application for all DOERS asynchronous work."""

from celery import Celery, bootsteps
from celery.schedules import crontab

from app.core.config import settings
from app.core.redis_production_readiness import validate_redis_production_settings


# P6: broker-facing production processes fail closed before Celery constructs a
# broker connection.  API processes are covered by deployment preflight and do
# not gain a second business-authority path through this guard.
if settings.is_production and settings.process_profile in {"worker", "maintenance", "beat"}:
    validate_redis_production_settings(settings)


WORKER_QUEUE = "worker"
# Historical queue label retained for deployment compatibility. The process is
# the isolated maintenance control plane and now hosts both lifecycle and
# narrowly bounded platform-maintenance tasks.
MAINTENANCE_QUEUE = "lifecycle-maintenance"
MAINTENANCE_TASKS = (
    "app.tasks.branch_lifecycle_sweeps.watchdog",
    "app.tasks.branch_lifecycle_sweeps.reconciliation",
    "app.tasks.branch_lifecycle_sweeps.notification_reconciliation",
    "app.tasks.external_effect_observability.snapshot",
    "app.tasks.runtime_observability.snapshot",
    "app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
    "app.tasks.platform_maintenance.advance_trial_lifecycles",
    "app.tasks.platform_maintenance.dispatch_organization_asset_jobs",
    "app.tasks.platform_maintenance.dispatch_organization_asset_cleanup",
    "app.tasks.platform_maintenance.reclaim_stale_idempotency",
    "app.tasks.platform_maintenance.archive_expired_idempotency",
    "app.tasks.platform_maintenance.geocoding_reverification",
    "app.tasks.platform_maintenance.cleanup_places_cache",
    "app.tasks.platform_maintenance.maintain_branch_audit_partitions",
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
    # P6-B: make the production reconnect contract explicit instead of
    # depending on Celery defaults.  A running worker retries indefinitely
    # after broker loss; publisher retries remain enabled; prefetch count is
    # reduced while the connection is recovering.
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=None,
    task_publish_retry=True,
    worker_enable_prefetch_count_reduction=True,
    task_always_eager=(settings.ENVIRONMENT == "development"),
    task_default_queue=WORKER_QUEUE,
    # Celery autodiscovery imports ``app.tasks.tasks``; it does not recursively
    # import sibling task modules.  Every module owning a task referenced by the
    # production Beat schedule must therefore be registered explicitly.  This
    # keeps the real Docker worker/maintenance commands executable without
    # test-only ``--include`` flags.
    imports=(
        "app.tasks.logos",
        "app.tasks.covers",
        "app.tasks.platform_maintenance",
        "app.tasks.external_effect_observability",
        "app.tasks.runtime_observability",
        "app.tasks.branch_hours_partition",
        "app.tasks.outbox_poller",
        "app.tasks.branch_outbox_poller",
        "app.tasks.branch_lifecycle_sweeps",
    ),
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
    "organization-asset-redispatch": {
        "task": "app.tasks.platform_maintenance.dispatch_organization_asset_jobs",
        "schedule": crontab(minute="*"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "organization-asset-cleanup-dispatch": {
        "task": "app.tasks.platform_maintenance.dispatch_organization_asset_cleanup",
        "schedule": crontab(minute="*"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    # Legacy reminder and daily-digest schedules remain intentionally absent.
    # Their prior implementations performed cross-tenant discovery and inline
    # external side effects. P4C first admits lifecycle member notifications
    # through the tenant-bound durable command/evidence pipeline.
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
    "notification-reconciliation-sweep": {
        "task": "app.tasks.branch_lifecycle_sweeps.notification_reconciliation",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "external-effect-operational-snapshot": {
        "task": "app.tasks.external_effect_observability.snapshot",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
    "p8-runtime-operational-snapshot": {
        "task": "app.tasks.runtime_observability.snapshot",
        "schedule": crontab(minute="*"),
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
    "branch-audit-partition-maintenance": {
        "task": "app.tasks.platform_maintenance.maintain_branch_audit_partitions",
        "schedule": crontab(hour=2, minute=45),
        "options": {"queue": MAINTENANCE_QUEUE},
    },
}

# P8 observability registers logging/context/metric signals only. It does not
# alter queues, acknowledgements, retries, prefetch, scheduler authority or the
# PostgreSQL source-of-truth semantics inherited from P5/P6.
from app.observability import celery_context as _p8_celery_observability  # noqa: E402,F401
