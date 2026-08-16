from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "app/core/celery_app.py"
ENTRYPOINT = ROOT / "app/tasks/celery_app.py"
COMPOSE = ROOT / "docker-compose.yml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deployment_entrypoint_exports_canonical_app_only() -> None:
    source = _source(ENTRYPOINT)
    tree = ast.parse(source)

    assert "from app.core.celery_app import celery_app" in source
    assert "app = celery_app" in source
    assert not any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "Celery"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "Celery"
        )
        for node in ast.walk(tree)
    )
    assert "beat_schedule" not in source


def test_canonical_app_has_crash_safe_worker_semantics() -> None:
    source = _source(CORE)

    assert 'Celery(\n    "doers"' in source
    assert "task_acks_late=True" in source
    assert "task_reject_on_worker_lost=True" in source
    assert "worker_prefetch_multiplier=1" in source
    assert "enable_utc=True" in source
    assert 'timezone="Asia/Kolkata"' in source
    assert 'task_always_eager=(settings.ENVIRONMENT == "development")' in source


def test_canonical_beat_schedule_contains_only_hardened_operational_sweeps() -> None:
    source = _source(CORE)

    required_tasks = {
        "app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
        "app.tasks.platform_maintenance.advance_trial_lifecycles",
        "app.tasks.platform_maintenance.dispatch_organization_asset_jobs",
        "app.tasks.platform_maintenance.dispatch_organization_asset_cleanup",
        "app.tasks.branch_hours_partition.run",
        "app.tasks.outbox_poller.run",
        "app.tasks.branch_outbox_poller.run",
        "app.tasks.branch_lifecycle_sweeps.watchdog",
        "app.tasks.branch_lifecycle_sweeps.reconciliation",
        "app.tasks.platform_maintenance.reclaim_stale_idempotency",
        "app.tasks.platform_maintenance.archive_expired_idempotency",
        "app.tasks.platform_maintenance.geocoding_reverification",
        "app.tasks.platform_maintenance.cleanup_places_cache",
    }
    forbidden_legacy_tasks = {
        "app.tasks.trial_tasks.monitor_trial_lifecycles",
        "app.tasks.expire_subs.run",
        "app.tasks.expire_subs.expire_subscriptions",
        "app.tasks.reminders.run",
        "app.tasks.reminders.send_daily_reminders",
        "app.tasks.daily_digest.run",
        "app.tasks.daily_digest.daily_digest",
        "app.tasks.logos.cleanup_orphaned_logos",
    }

    for task_name in required_tasks:
        assert task_name in source
    for task_name in forbidden_legacy_tasks:
        assert task_name not in source


def test_maintenance_tasks_are_routed_to_a_dedicated_queue() -> None:
    source = _source(CORE)

    assert 'WORKER_QUEUE = "worker"' in source
    assert 'MAINTENANCE_QUEUE = "lifecycle-maintenance"' in source
    assert "task_default_queue=WORKER_QUEUE" in source
    assert "task_routes={" in source
    for task_name in (
        "app.tasks.branch_lifecycle_sweeps.watchdog",
        "app.tasks.branch_lifecycle_sweeps.reconciliation",
        "app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
        "app.tasks.platform_maintenance.advance_trial_lifecycles",
        "app.tasks.platform_maintenance.dispatch_organization_asset_jobs",
        "app.tasks.platform_maintenance.dispatch_organization_asset_cleanup",
    ):
        assert f'"{task_name}"' in source
    assert '"options": {"queue": MAINTENANCE_QUEUE}' in source


def test_deployment_separates_worker_and_maintenance_processes() -> None:
    source = _source(COMPOSE)

    assert "celery-worker:" in source
    assert "celery-maintenance-worker:" in source
    assert "CELERY_WORKER_PROFILE: worker" in source
    assert "CELERY_WORKER_PROFILE: maintenance" in source
    assert "-Q worker" in source
    assert "-Q lifecycle-maintenance" in source
    assert 'AUTH_DATABASE_URL: ""' in source
    assert 'MAINTENANCE_DATABASE_URL: ""' in source
    assert 'WORKER_DATABASE_URL: ""' in source
