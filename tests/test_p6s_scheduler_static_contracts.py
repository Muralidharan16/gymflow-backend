from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "app/core/celery_beat_owner.py"
ENTRYPOINT = ROOT / "app/tasks/celery_app.py"
BASE_COMPOSE = ROOT / "docker-compose.yml"
IDENTITIES = ROOT / "deploy/docker-compose.production-identities.yml"
SCHEDULER_OVERLAY = ROOT / "deploy/docker-compose.p6s-scheduler.yml"
RUNTIME = ROOT / "tests/test_p6s_scheduler_runtime.py"
RUNTIME_FIXTURE = ROOT / "scripts/ci/p6s_runtime_fixture.py"
WORKFLOW = ROOT / ".github/workflows/p6s-scheduler-ownership-pg16.yml"
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
    assert source.count("if not self._doers_ownership.ensure_owned()") >= 2
    assert "redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2], 'NX')" in source
    assert "redis.call('PEXPIRE', KEYS[1], ARGV[2])" in source


def test_production_entrypoint_selects_owned_scheduler() -> None:
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "from app.core.config import settings" in source
    assert "if settings.is_production:" in source
    assert "app.core.celery_beat_owner:DoersOwnedPersistentScheduler" in source


def test_production_beat_remains_standalone_and_database_free() -> None:
    base = yaml.safe_load(BASE_COMPOSE.read_text(encoding="utf-8"))
    identities = yaml.safe_load(IDENTITIES.read_text(encoding="utf-8"))
    overlay = yaml.safe_load(SCHEDULER_OVERLAY.read_text(encoding="utf-8"))

    beat_command = base["services"]["celery-beat"]["command"]
    assert " beat " in f" {beat_command} "
    for worker in ("celery-worker", "celery-maintenance-worker"):
        assert " -B " not in f" {base['services'][worker]['command']} "

    beat_env = identities["services"]["celery-beat"]["environment"]
    for key in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "WORKER_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
    ):
        assert beat_env[key] == ""

    assert set(overlay["services"]["celery-beat"]["environment"]) == {
        "CELERY_BEAT_OWNERSHIP_KEY",
        "CELERY_BEAT_OWNERSHIP_TTL_SECONDS",
        "CELERY_BEAT_OWNERSHIP_RETRY_SECONDS",
    }


def test_runtime_forces_real_owner_transfer_and_duplicate_publication() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    fixture = RUNTIME_FIXTURE.read_text(encoding="utf-8")

    for marker in (
        "P6S_BEAT_DATABASE_CREDENTIALS_ABSENT=PASS",
        "P6S_SINGLE_ACTIVE_BEAT=PASS",
        "P6S_STALE_OWNER_STOPS_PUBLISHING=PASS",
        "P6S_OWNERSHIP_RECOVERY_AFTER_OWNER_DEATH=PASS",
        "P6S_FORCED_DUPLICATE_PERIODIC_PUBLICATION=PASS",
        "P6S_DUPLICATE_WORKER_DELIVERY=PASS",
        "P6S_SINGLE_AUTHORITATIVE_EFFECT=PASS",
        "P6S_DURABLE_TERMINAL_STATE=PASS",
    ):
        assert marker in runtime

    assert "--scheduler=celery.beat:PersistentScheduler" in runtime
    assert "--pool=prefork" in runtime
    assert "attempt_count" in runtime
    assert 'terminal[1] == 1' in runtime
    assert "redis-server" in fixture
    assert '"tls-port 16382' in fixture
    assert '"tls-port 16383' in fixture
    assert "NOBYPASSRLS" in fixture
    assert "verify_redis_production_readiness.py" in fixture
    assert '"upgrade", "head"' in fixture


def test_p6s_workflow_is_exact_head_and_terminal_marker_bound() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [
        "hardening/p6-celery-redis-scheduler-production-readiness"
    ]
    assert "workflow_call" in workflow["on"]
    assert 'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"' in source
    assert "scripts.ci.p6s_runtime_fixture" in source
    assert "tests/test_p6s_scheduler_runtime.py" in source
    for marker in (
        "P6_BEAT_SINGLE_OWNER=PASS",
        "P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS",
        "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
        "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in source


def test_p6s_matches_frozen_governance() -> None:
    governance = GOVERNANCE.read_text(encoding="utf-8")
    assert "Only one Beat scheduler may own the DOers schedule at a time." in governance
    assert "Redis ownership can never become business authority" in governance
    assert "duplicate periodic messages" in governance
