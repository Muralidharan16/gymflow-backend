from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CELERY = ROOT / "app" / "core" / "celery_app.py"
RUNTIME = ROOT / "tests" / "test_p6w_worker_shutdown_redelivery_runtime.py"
HOOK = ROOT / "scripts" / "ci" / "p6w_sigterm_fault_hooks.py"
WORKFLOW = ROOT / ".github" / "workflows" / "p6w-worker-shutdown-redelivery-pg16.yml"
MATRIX = ROOT / "docs" / "architecture" / "P6_ACCEPTANCE_MATRIX.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p6w_preserves_required_celery_delivery_contract() -> None:
    celery = _text(CELERY)
    assert "task_acks_late=True" in celery
    assert "task_reject_on_worker_lost=True" in celery
    assert "worker_prefetch_multiplier=1" in celery


def test_p6w_runtime_uses_real_prefork_worker_and_main_process_sigterm() -> None:
    runtime = _text(RUNTIME)
    assert '"--pool=prefork"' in runtime
    assert "worker.process.terminate()" in runtime
    assert "sigterm_hold_entered" in runtime
    assert "Warm shutdown" in runtime
    assert "replacement worker" in runtime.lower()


def test_p6w_redelivery_reuses_certified_process_faults_freshly_under_p6() -> None:
    runtime = _text(RUNTIME)
    assert "test_real_worker_death_before_commit_is_reclaimed_by_replacement" in runtime
    assert "test_real_worker_death_after_commit_redelivers_without_repeating_effect" in runtime
    assert "P6W_BEFORE_COMMIT_REDELIVERY=PASS" in runtime
    assert "P6W_AFTER_COMMIT_PRE_ACK_REDELIVERY=PASS" in runtime
    assert "P6W_SINGLE_AUTHORITATIVE_EFFECT=PASS" in runtime


def test_p6w_worker_receives_only_reduced_worker_database_authority() -> None:
    runtime = _text(RUNTIME)
    assert '"ENVIRONMENT": "production"' in runtime
    assert '"DOERS_PROCESS_PROFILE": "worker"' in runtime
    assert '"CELERY_WORKER_PROFILE": "worker"' in runtime
    assert '"DATABASE_URL"' in runtime
    assert '"TEST_ADMIN_DATABASE_URL"' in runtime
    assert '"P6W_APP_DATABASE_URL"' in runtime
    assert "environment.pop(forbidden, None)" in runtime
    assert "WORKER_DATABASE_URL" in _text(WORKFLOW)


def test_p6w_sigterm_hook_is_ci_only_and_does_not_patch_production_imports() -> None:
    hook = _text(HOOK)
    celery = _text(CELERY)
    assert "worker_process_init" in hook
    assert "P6W_TARGET_EVENT_ID" in hook
    assert "p6w_sigterm_fault_hooks" not in celery


def test_p6w_workflow_requires_pg16_tls_redis_and_production_preflight() -> None:
    workflow = _text(WORKFLOW)
    for token in (
        "install_pg16_test_stack.sh",
        "redis:7-alpine",
        "tls-port 6379",
        "requirepass",
        "appendonly yes",
        "appendfsync everysec",
        "maxmemory-policy noeviction",
        "verify_redis_production_readiness.py",
        "ssl_cert_reqs=required",
        "P6_WORKER_SIGTERM_SAFE=PASS",
        "P6_LATE_ACK_REDELIVERY_SAFE=PASS",
        "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert token in workflow


def test_frozen_acceptance_matrix_requires_real_sigterm_and_redelivery_evidence() -> None:
    matrix = _text(MATRIX)
    assert "Real prefork worker receives `SIGTERM`" in matrix
    assert "before-commit and after-commit/before-ack fault points" in matrix
    assert "mocks as the sole evidence" in matrix
