from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "app/core/celery_beat_owner.py"
GOVERNANCE = ROOT / "docs/architecture/P6_CELERY_REDIS_SCHEDULER_SCOPE_AND_GATES.md"


def test_owned_scheduler_exists_and_remains_redis_only() -> None:
    source = SCHEDULER.read_text(encoding="utf-8")
    assert "DoersOwnedPersistentScheduler" in source
    assert "BeatOwnershipLease" in source
    assert "CELERY_BEAT_OWNERSHIP_KEY" in source
    assert "CELERY_BEAT_OWNERSHIP_TTL_SECONDS" in source
    assert "CELERY_BEAT_OWNERSHIP_RETRY_SECONDS" in source
    assert "DATABASE_URL" not in source
    assert "psycopg" not in source
    assert "sqlalchemy" not in source.lower()


def test_scheduler_rejects_embedded_beat_and_rechecks_ownership() -> None:
    source = SCHEDULER.read_text(encoding="utf-8")
    assert 'settings.process_profile != "beat"' in source
    assert "embedded worker -B is forbidden" in source
    assert "def tick" in source
    assert "def apply_entry" in source
    assert "if not self._doers_ownership.ensure_owned()" in source


def test_p6s_matches_frozen_governance() -> None:
    governance = GOVERNANCE.read_text(encoding="utf-8")
    assert "Only one Beat scheduler may own the DOers schedule at a time." in governance
    assert "Redis ownership can never become business authority" in governance
    assert "duplicate periodic messages" in governance
