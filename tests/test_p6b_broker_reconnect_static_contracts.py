from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CELERY_APP = ROOT / "app/core/celery_app.py"
RUNTIME = ROOT / "tests/test_p6b_broker_reconnect_runtime.py"
WORKFLOW = ROOT / ".github/workflows/p6b-broker-reconnect-pg16.yml"


def test_celery_reconnect_policy_is_explicit_and_unbounded_for_running_workers() -> None:
    source = CELERY_APP.read_text(encoding="utf-8")
    for phrase in (
        "broker_connection_retry_on_startup=True",
        "broker_connection_retry=True",
        "broker_connection_max_retries=None",
        "task_publish_retry=True",
        "worker_enable_prefetch_count_reduction=True",
        "task_acks_late=True",
        "task_reject_on_worker_lost=True",
        "worker_prefetch_multiplier=1",
    ):
        assert phrase in source


def test_runtime_uses_real_tls_broker_and_same_live_worker_across_restart() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'P6B_PROCESS_FAULTS") != "1"',
        'P6B_DISPOSABLE_DATABASE") != _DATABASE',
        'broker.scheme != "rediss"',
        '"docker", *args',
        '"stop", "-t", "1", "p6b-primary"',
        '"start", "p6b-primary"',
        "original_pid = worker.process.pid",
        "assert worker.process.pid == original_pid",
        "same live Celery worker reconnect after Redis restart",
        "pre-outage",
        "during-outage",
        "dead_lettered",
        "P6B_NO_FALSE_SUCCESS=PASS",
    ):
        assert phrase in source
    assert "monkeypatch" not in source
    assert "unittest.mock" not in source


def test_worker_subprocess_keeps_only_worker_database_authority() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for forbidden in (
        '"DATABASE_URL",',
        '"AUTH_DATABASE_URL",',
        '"MAINTENANCE_DATABASE_URL",',
        '"FINANCE_CONFIG_DATABASE_URL",',
    ):
        assert forbidden in source
    forbidden_block = source[source.index("for forbidden in ("):source.index("environment.update(")]
    assert '"WORKER_DATABASE_URL",' not in forbidden_block
    assert '"DOERS_PROCESS_PROFILE": "worker"' in source
    assert '"CELERY_WORKER_PROFILE": "worker"' in source


def test_workflow_is_real_pg16_tls_redis_same_head_and_fail_closed() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for phrase in (
        "ubuntu-24.04",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "worker_p6b_runtime",
        "NOBYPASSRLS",
        "rediss://",
        "appendonly yes",
        "appendfsync everysec",
        "maxmemory-policy noeviction",
        "replicaof p6b-primary 6379",
        "tests/test_p6b_broker_reconnect_runtime.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P6_BROKER_RECONNECT_RECOVERY=PASS",
        "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert phrase in source
    assert "continue-on-error" not in source


def test_p6b_does_not_claim_later_phase_or_refund_execution() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (CELERY_APP, RUNTIME, WORKFLOW)
    )
    for forbidden in (
        "refund provider call",
        "p6-w certified",
        "p6-p certified",
        "p6-s certified",
        "p6-f certified",
        "worker_cancel_long_running_tasks_on_connection_loss=true",
    ):
        assert forbidden not in combined
