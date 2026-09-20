from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "app/finance_core/services/refund_provider_worker.py"
CELERY = ROOT / "app/core/celery_app.py"
BINDINGS = ROOT / "security/runtime_identity/runtime_bindings.v1.json"
PROFILES = ROOT / "security/runtime_identity/process_profiles.v1.json"
ARCH = ROOT / "docs/architecture/PAY10_REFUND_CREDIT_NOTE_PROVIDER_EXECUTION.md"
MATRIX = ROOT / "docs/architecture/PAY10_ACCEPTANCE_MATRIX.md"
CONTRACT = ROOT / "docs/architecture/pay10_refund_provider_execution_v1.json"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay10e_processor_commits_before_and_after_provider_io():
    source = _text(WORKER)
    assert "await session.commit()" in source
    assert "response = await provider.submit_refund" in source
    assert "after_bind_commit" in source
    assert "after_provider_effect" in source
    assert "after_outcome_commit" in source
    assert "keeping provider calls outside database transactions" not in source
    assert "provider I/O runs with no open" in source


def test_pay10e_provider_success_never_becomes_financial_success_in_worker():
    source = _text(WORKER)
    assert "finalize_pay10_refund" not in source
    assert "FinanceRefundFinancialFinalizationService" not in source
    assert "ledger_entries" not in source
    assert "outbox_events" not in source
    assert "status='succeeded'" not in source


def test_pay10e_unknown_outcome_and_provider_failure_are_durable():
    source = _text(WORKER)
    assert "error.requires_reconciliation" in source
    assert "record_unknown" in source
    assert "record_failure" in source
    assert "permanent=not error.automatic_retry_allowed" in source


def test_pay10e_unexpected_db_ack_failure_is_not_swallowed():
    source = _text(WORKER)
    outcome = source.split("async def _record_outcome", 1)[1]
    outcome = outcome.split("async def run_once", 1)[0]
    assert "except BaseException" in outcome
    assert "await session.rollback()" in outcome
    assert "raise" in outcome
    assert "return" not in outcome


def test_pay10e_inherits_real_late_ack_redelivery_policy():
    celery = _text(CELERY)
    for phrase in (
        "task_acks_late=True",
        "task_reject_on_worker_lost=True",
        "worker_prefetch_multiplier=1",
        "broker_connection_retry=True",
        "task_publish_retry=True",
    ):
        assert phrase in celery


def test_pay10e_does_not_activate_reserved_refund_runtime_in_production():
    bindings = _text(BINDINGS)
    profiles = _text(PROFILES)
    assert '"finance_refund_runtime"' in bindings
    assert '"reserved_unbound_capabilities"' in bindings
    assert '"FINANCE_REFUND_DATABASE_URL"' not in bindings
    assert '"FINANCE_REFUND_DATABASE_URL"' not in profiles
    assert "refund_provider_worker" not in _text(CELERY)


def test_pay10e_architecture_keeps_live_provider_and_money_disabled():
    architecture = " ".join(_text(ARCH).split())
    matrix = _text(MATRIX)
    contract = _text(CONTRACT)
    assert "PAY-10-E worker and crash recovery" in architecture
    assert "P10-E" in matrix
    assert '"live_provider": false' in contract
    assert '"live_money_movement": false' in contract
