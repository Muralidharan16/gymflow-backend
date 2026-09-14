from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
RUNTIME = ROOT / "tests" / "test_p6p_poison_message_runtime.py"
HOOK = ROOT / "scripts" / "ci" / "p6p_poison_fault_hooks.py"
WORKFLOW = ROOT / ".github" / "workflows" / "p6p-poison-message-pg16.yml"
MATRIX = ROOT / "docs" / "architecture" / "P6_ACCEPTANCE_MATRIX.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_production_poller_has_bounded_retry_and_terminal_dead_letter_contract() -> None:
    poller = _text(POLLER)
    for token in (
        'exhausted = permanent or attempts >= max_attempts',
        "delay_seconds = min(1800, 30 * (2 ** max(attempts - 1, 0)))",
        "SET status = 'dead_lettered'",
        "SET status = 'pending'",
        "AND attempt_count < max_attempts",
        "FOR UPDATE SKIP LOCKED",
        "for event in events:",
        "outcome = await _process_event(event, worker_id)",
    ):
        assert token in poller


def test_runtime_uses_real_prefork_worker_and_real_unregistered_broker_message() -> None:
    runtime = _text(RUNTIME)
    for token in (
        '"--pool=prefork"',
        "p6p.unregistered.",
        "Received unregistered task",
        "_queue_depth(worker.queue) == 0",
        "assert worker.process.pid == original_pid",
        "rediss",
        "P6P_UNREGISTERED_BROKER_MESSAGE_DISCARDED=PASS",
        "P6P_WORKER_FLEET_SURVIVES_POISON=PASS",
    ):
        assert token in runtime
    assert "monkeypatch" not in runtime
    assert "unittest.mock" not in runtime


def test_valid_durable_poison_retries_then_stays_terminal() -> None:
    runtime = _text(RUNTIME)
    for token in (
        "'branch.lifecycle_saga'",
        "'from_status','active'",
        "'to_status','temporarily_closed'",
        "'p6p_deterministic_failure',true",
        "2,%s",
        'first_state[0] == "pending"',
        'terminal_valid[0] == "dead_lettered"',
        "assert _state(valid_poison) == terminal_valid",
        "P6P_VALID_DURABLE_POISON_RETRY_BOUNDED=PASS",
        "P6P_TERMINAL_POISON_NOT_REDISPATCHED=PASS",
        "P6P_BATCH_PROGRESS_AFTER_POISON=PASS",
    ):
        assert token in runtime


def test_poison_fault_hook_is_ci_only_retryable_and_not_in_production_imports() -> None:
    hook = _text(HOOK)
    celery = _text(ROOT / "app" / "core" / "celery_app.py")
    assert "worker_process_init" in hook
    assert 'payload.get("p6p_deterministic_failure") is True' in hook
    assert "permanent=False" in hook
    assert "P6P_POISON_FAULTS" in hook
    assert "p6p_poison_fault_hooks" not in celery


def test_worker_subprocess_strips_non_worker_database_and_runtime_secrets() -> None:
    runtime = _text(RUNTIME)
    for token in (
        '"DATABASE_URL",',
        '"AUTH_DATABASE_URL",',
        '"MAINTENANCE_DATABASE_URL",',
        '"FINANCE_CONFIG_DATABASE_URL",',
        '"TEST_ADMIN_DATABASE_URL",',
        '"P6P_APP_DATABASE_URL",',
        '"MIGRATION_PASSWORD",',
        '"AUTH_RUNTIME_PASSWORD",',
        '"APP_RUNTIME_PASSWORD",',
        '"WORKER_RUNTIME_PASSWORD",',
        '"DOERS_PROCESS_PROFILE": "worker"',
        '"CELERY_WORKER_PROFILE": "worker"',
    ):
        assert token in runtime


def test_workflow_is_real_pg16_tls_redis_same_head_and_fail_closed() -> None:
    source = _text(WORKFLOW)
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for token in (
        "ubuntu-24.04",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "worker_p6p_runtime",
        "NOBYPASSRLS",
        "rediss://",
        "appendonly yes",
        "appendfsync everysec",
        "maxmemory-policy noeviction",
        "replicaof p6p-primary 6379",
        "verify_redis_production_readiness.py",
        "tests/test_p6p_poison_message_runtime.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P6_POISON_MESSAGE_CONTAINED=PASS",
        "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
        "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert token in source
    assert "continue-on-error" not in source


def test_frozen_acceptance_matrix_requires_both_poison_classes() -> None:
    matrix = _text(MATRIX)
    assert "malformed/unregistered message plus repeatedly failing valid durable command" in matrix
    assert "without hot loop or loss" in matrix
    assert "P6_POISON_MESSAGE_CONTAINED=PASS" in matrix
