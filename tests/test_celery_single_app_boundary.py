from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "app/core/celery_app.py"
ENTRYPOINT = ROOT / "app/tasks/celery_app.py"


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


def test_canonical_beat_schedule_contains_all_operational_sweeps() -> None:
    source = _source(CORE)

    required_tasks = {
        "app.tasks.trial_tasks.monitor_trial_lifecycles",
        "app.tasks.expire_subs.run",
        "app.tasks.reminders.run",
        "app.tasks.daily_digest.run",
        "app.tasks.logos.cleanup_orphaned_logos",
        "app.tasks.branch_hours_partition.run",
        "app.tasks.outbox_poller.run",
        "app.tasks.branch_outbox_poller.run",
        "app.tasks.branch_lifecycle_sweeps.watchdog",
        "app.tasks.branch_lifecycle_sweeps.reconciliation",
    }
    for task_name in required_tasks:
        assert task_name in source
