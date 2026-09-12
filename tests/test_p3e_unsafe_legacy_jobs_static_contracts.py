from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CELERY = ROOT / "app/core/celery_app.py"
TRIAL = ROOT / "app/tasks/trial_tasks.py"
REMINDERS = ROOT / "app/tasks/reminders.py"
DIGEST = ROOT / "app/tasks/daily_digest.py"

_UNSAFE_SCHEDULED_TASKS = {
    "app.tasks.trial_tasks.monitor_trial_lifecycles",
    "send_daily_reminders",
    "app.tasks.daily_digest.run",
    "app.tasks.logos.cleanup_orphaned_logos",
}


def _literal_strings(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_legacy_cross_tenant_jobs_are_not_in_beat_schedule() -> None:
    source = CELERY.read_text()
    tree = ast.parse(source)

    schedule_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for value in node.value.values:
            if not isinstance(value, ast.Dict):
                continue
            for key_node, value_node in zip(value.keys, value.values):
                if (
                    isinstance(key_node, ast.Constant)
                    and key_node.value == "task"
                    and isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, str)
                ):
                    schedule_names.add(value_node.value)

    assert not (_UNSAFE_SCHEDULED_TASKS & schedule_names)
    assert "app.tasks.platform_maintenance.advance_trial_lifecycles" in schedule_names
    assert (
        "app.tasks.platform_maintenance.expire_legacy_member_subscriptions"
        in schedule_names
    )


def test_legacy_trial_entry_point_is_fail_closed_without_worker_database_access() -> None:
    source = TRIAL.read_text()
    assert "worker_async_session_maker" not in source
    assert "WorkerSyncSessionLocal" not in source
    assert "TrialSubscription" not in source
    assert "raise RuntimeError" in source
    assert "app.tasks.trial_tasks.monitor_trial_lifecycles" in _literal_strings(TRIAL)


def test_legacy_notification_jobs_are_fail_closed_without_external_delivery() -> None:
    reminder_source = REMINDERS.read_text()
    for forbidden in (
        "worker_async_session_maker",
        "WorkerSyncSessionLocal",
        "send_whatsapp_message",
        "MemberRepository",
        "SubscriptionRepository",
    ):
        assert forbidden not in reminder_source
    assert reminder_source.count("raise RuntimeError") >= 2
    assert "send_daily_reminders" in _literal_strings(REMINDERS)
    assert "send_birthday_wishes" in _literal_strings(REMINDERS)


def test_legacy_daily_digest_is_fail_closed_without_global_discovery() -> None:
    source = DIGEST.read_text()
    for forbidden in (
        "worker_async_session_maker",
        "WorkerSyncSessionLocal",
        "select(",
        "Gym",
        "generate_digest_pdf",
    ):
        assert forbidden not in source
    assert "raise RuntimeError" in source
    assert "app.tasks.daily_digest.run" in _literal_strings(DIGEST)


def test_orphan_cleanup_remains_out_of_schedule_until_bounded_cleanup_exists() -> None:
    assert "app.tasks.logos.cleanup_orphaned_logos" not in CELERY.read_text()
