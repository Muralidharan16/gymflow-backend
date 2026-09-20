from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / (
    "alembic/versions/"
    "zv07d8e9f0a56_pay10_refund_financial_finalization.py"
)
SERVICE = (
    ROOT / "app/finance_core/services/refund_financial_finalization.py"
)
MODEL = ROOT / "app/finance_core/models/foundation.py"
ARCH = ROOT / "docs/architecture/PAY10_REFUND_CREDIT_NOTE_PROVIDER_EXECUTION.md"
MATRIX = ROOT / "docs/architecture/PAY10_ACCEPTANCE_MATRIX.md"
CONTRACT = ROOT / "docs/architecture/pay10_refund_provider_execution_v1.json"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay10d_is_additive_after_certified_c_revision():
    source = _text(MIGRATION)
    assert 'revision = "zv07d8e9f0a56"' in source
    assert 'down_revision = "zu07d8e9f0a55"' in source
    assert "CREATE UNIQUE INDEX uq_pay10_refund_ledger_source" in source
    assert "DROP INDEX finance.uq_pay10_refund_ledger_source" in source


def test_pay10d_credit_note_binding_is_issued_accounting_provenance_only():
    source = _text(MIGRATION)
    assert (
        "CREATE FUNCTION app_secure.link_pay10_refund_credit_note"
        in source
    )
    assert "cn.status<>'issued'" in source
    assert "cn.credit_note_number IS NULL" in source
    assert "cn.issued_at IS NULL" in source
    assert "credit note invoice was not paid by refund payment" in source
    assert "credit note lacks posted accounting reversal" in source
    assert "v_debits<>v_cn_amount" in source
    assert "v_credits<>v_cn_amount" in source
    assert "v_ar_credit_count<>1" in source
    assert "least(" in source
    assert "v_refund.amount-v_backing" in source
    assert "INSERT INTO finance.refund_credit_note_links" in source
    assert "INSERT INTO finance.credit_notes" not in source
    assert "UPDATE finance.credit_notes" not in source


def test_pay10d_finalizer_requires_processed_evidence_and_exact_backing():
    source = _text(MIGRATION)
    assert "CREATE FUNCTION app_secure.finalize_pay10_refund" in source
    assert "v_command.status<>'reconciliation_pending'" in source
    assert "e.normalized_status='processed'" in source
    assert (
        "e.evidence_sha256=v_command.provider_evidence_sha256"
        in source
    )
    assert "v_backing<>v_refund.amount" in source
    assert "v_invalid_backing<>0" in source
    assert "v_successful>v_allocated" in source
    assert "v_successful>v_payment.amount" in source


def test_pay10d_cash_refund_ledger_never_reverses_revenue_again():
    source = _text(MIGRATION)
    assert "'refund'," in source
    assert "'Provider cash refund receivable restoration'" in source
    assert "'Provider cash refund clearing'" in source
    assert "la.code='AR'" in source
    assert "la.code='PAYMENT_CLEARING'" in source
    finalizer = source.split(
        "CREATE FUNCTION app_secure.finalize_pay10_refund", 1
    )[1]
    assert "code='REVENUE'" not in finalizer
    assert "code='CGST'" not in finalizer
    assert "code='SGST'" not in finalizer
    assert "code='IGST'" not in finalizer


def test_pay10d_terminal_effects_are_one_atomic_database_capability():
    source = _text(MIGRATION)
    finalizer = source.split(
        "CREATE FUNCTION app_secure.finalize_pay10_refund", 1
    )[1]
    assert "INSERT INTO finance.ledger_entries" in finalizer
    assert "INSERT INTO finance.ledger_entry_lines" in finalizer
    assert finalizer.count("INSERT INTO finance.outbox_events") == 3
    assert "finance.refund.completed" in finalizer
    assert "finance.payment.refund_state.changed" in finalizer
    assert "finance.ledger.entry.posted" in finalizer
    assert "UPDATE finance.refunds" in finalizer
    assert "UPDATE finance.payments" in finalizer
    assert "UPDATE finance.refund_execution_commands" in finalizer
    assert "status='succeeded'" in finalizer
    assert "completed_at=pg_catalog.clock_timestamp()" in finalizer


def test_pay10d_replay_is_bound_to_one_posted_refund_ledger():
    source = _text(MIGRATION)
    assert "uq_pay10_refund_ledger_source" in source
    assert "IF v_command.status='succeeded' THEN" in source
    assert "v_ledger_count<>1" in source
    assert "v_line_count<>2" in source
    assert "v_ar_debit_count<>1" in source
    assert "v_clearing_credit_count<>1" in source
    assert "true;" in source


def test_pay10d_runtime_has_capabilities_not_raw_finance_dml():
    source = _text(MIGRATION)
    assert (
        "GRANT EXECUTE ON FUNCTION {signature} "
        in source
    )
    assert "TO finance_refund_runtime" in source
    assert "PAY-10-D direct Finance DML leaked" in source
    for role in (
        "app_runtime",
        "worker_runtime",
        "finance_reconciliation_runtime",
    ):
        assert role in source


def test_pay10d_acl_broadening_is_exactly_reversible():
    source = _text(MIGRATION)
    assert "CREATE TABLE app_private.pay10d_acl_delta" in source
    assert "pg_catalog.has_column_privilege" in source
    assert "INSERT INTO app_private.pay10d_acl_delta" in source
    assert "REVOKE {privilege} ({column_name})" in source
    assert "DROP TABLE app_private.pay10d_acl_delta" in source
    assert "PAY-10-D unsafe ACL delta identity" in source


def test_pay10d_populated_downgrade_fails_closed():
    source = _text(MIGRATION)
    assert "def _has_finalization_evidence" in source
    assert "status='succeeded'" in source
    assert "completed_at IS NOT NULL" in source
    assert (
        "PAY-10-D downgrade blocked: finalized refund effects exist"
        in source
    )


def test_pay10d_service_is_transaction_only_and_cannot_issue_credit_notes():
    source = _text(SERVICE).lower()
    assert "class financerefundfinancialfinalizationservice:" in source
    assert "does no provider network i/o" in source
    assert "does not issue credit notes" in source
    assert "does not commit" in source
    assert "app_secure.link_pay10_refund_credit_note" in source
    assert "app_secure.finalize_pay10_refund" in source
    assert ".commit(" not in source
    assert "httpx" not in source
    assert "requests" not in source
    assert "razorpay" not in source


def test_pay10d_model_contract_has_refund_ledger_uniqueness():
    source = _text(MODEL)
    assert '"uq_pay10_refund_ledger_source"' in source
    assert '"source_type = \'refund\' AND status = \'posted\'"' in source


def test_pay10d_architecture_keeps_live_money_movement_disabled():
    architecture = _text(ARCH)
    matrix = _text(MATRIX)
    contract = _text(CONTRACT)
    assert "D — financial finalization" in architecture
    assert "Revenue and tax reversal remain credit-note responsibility" in architecture
    assert "P10-D" in matrix
    assert '"live_provider": false' in contract
    assert '"live_money_movement": false' in contract
