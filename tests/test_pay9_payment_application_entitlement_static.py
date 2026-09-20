from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zs07d8e9f0a53_pay9_payment_application_entitlement.py"
SERVICE = ROOT / "app/finance_core/services/payment_settlement.py"
WEBHOOK = ROOT / "app/finance_core/services/razorpay_webhooks.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay9_migration_is_exact_successor_and_has_fail_closed_lifecycle():
    source = _text(MIGRATION)
    assert 'revision = "zs07d8e9f0a53"' in source
    assert 'down_revision = "zr07d8e9f0a52"' in source
    assert "payment_application_records" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "populated downgrade refused" in source
    assert "NO FORCE ROW LEVEL SECURITY" in source


def test_pay9_application_derives_authority_instead_of_accepting_invoice_or_amount():
    source = _text(MIGRATION)
    signature = source.split(
        "CREATE FUNCTION app_secure.apply_verified_provider_payment(", 1
    )[1].split(")", 1)[0]
    assert "p_payment_id uuid" in signature
    assert "p_payment_event_id uuid" in signature
    assert "p_invoice_id" not in signature
    assert "p_amount" not in signature
    assert "member_subscription_checkout_bindings" in source
    assert "member_subscription_finance_bindings" in source
    assert "'payment.captured','order.paid'" in source
    assert "v_payment.status NOT IN ('captured','settled')" in source


def test_pay9_supports_partial_overpayment_and_unapplied_outcomes_without_second_ledger():
    source = _text(MIGRATION)
    assert "LEAST(" in source
    assert "GREATEST(" in source
    assert "v_payment_available" in source
    assert "v_invoice_outstanding" in source
    assert "'applied_paid'" in source
    assert "'applied_partial'" in source
    assert "'unapplied_no_checkout_binding'" in source
    assert "'unapplied_invoice_already_paid'" in source
    assert "'unapplied_currency_mismatch'" in source
    assert "finance.payment_allocations" in source
    assert "finance.ledger_entries" in source
    assert "finance.ledger_entry_lines" in source
    assert "'finance.payment.applied'" in source
    assert "'finance.invoice.paid'" in source
    assert "'finance.invoice.partially_paid'" in source
    assert "'finance.ledger.entry.posted'" in source
    assert "CREATE TABLE finance.ledger_" not in source


def test_pay9_runtime_roles_receive_capability_not_table_dml():
    source = _text(MIGRATION)
    assert "GRANT EXECUTE ON FUNCTION" in source
    assert "TO finance_payment_runtime" in source
    assert "TO app_runtime" in source
    for grant in (
        "GRANT SELECT ON TABLE finance.payment_application_records TO app_runtime",
        "GRANT INSERT ON TABLE finance.payment_application_records TO app_runtime",
        "GRANT UPDATE ON TABLE finance.payment_application_records TO app_runtime",
        "GRANT SELECT ON TABLE finance.payment_application_records TO finance_payment_runtime",
        "GRANT INSERT ON TABLE finance.payment_application_records TO finance_payment_runtime",
    ):
        assert grant not in source


def test_pay9_service_has_no_caller_selected_financial_target():
    source = _text(SERVICE)
    assert "payment_id: uuid.UUID" in source
    assert "payment_event_id: uuid.UUID" in source
    assert "invoice_id:" not in source.split(
        "async def apply_verified_provider_payment", 1
    )[1].split(") ->", 1)[0]
    assert "amount:" not in source.split(
        "async def apply_verified_provider_payment", 1
    )[1].split(") ->", 1)[0]
    assert "app_secure.apply_verified_provider_payment" in source


def test_pay9_is_wired_only_after_pay8_provider_evidence_confirmation():
    source = _text(WEBHOOK)
    confirmation = source.index(
        "await self._confirmation.confirm_provider_evidence"
    )
    settlement = source.index(
        "await self._settlement.apply_verified_provider_payment"
    )
    assert confirmation < settlement
    assert 'if result.payment_status in {"captured", "settled"}' in source


def test_pay9_does_not_enable_live_money_refund_or_entitlement_direct_mutation():
    combined = "\n".join(
        _text(path).lower()
        for path in (MIGRATION, SERVICE, WEBHOOK)
    )
    assert "rzp_live_" not in combined
    assert "refund_execution" not in _text(SERVICE).lower()
    assert "activate_subscription" not in combined
    assert "update public.subscription_terms" not in combined
