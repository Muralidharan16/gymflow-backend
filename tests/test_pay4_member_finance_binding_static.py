from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zo07d8e9f0a49_pay4_member_finance_binding.py"
SERVICE = ROOT / "app/services/member_subscription_v2_service.py"
REPO = ROOT / "app/repositories/member_subscription_v2_repo.py"
CHECKOUT = ROOT / "app/finance_core/services/member_subscription_checkout.py"
CONTRACT = ROOT / "docs/architecture/pay4_member_finance_binding_v1.json"


def test_pay4_is_exactly_additive_over_certified_pay3():
    source=MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zo07d8e9f0a49"' in source
    assert 'down_revision = "zn07d8e9f0a48"' in source
    assert "CREATE TABLE finance.payment_contexts" in source
    assert "CREATE TABLE finance.member_subscription_finance_bindings" in source
    for forbidden in (
        "DROP TABLE public.subscription_terms",
        "ALTER TABLE finance.invoices ADD COLUMN",
        "UPDATE finance.invoices SET",
        "DELETE FROM finance.payments",
        "TRUNCATE TABLE",
    ):
        assert forbidden not in source


def test_pay4_binding_contains_required_canonical_fields_and_is_immutable():
    source=MIGRATION.read_text(encoding="utf-8")
    for token in (
        "subscription_term_id UUID NOT NULL",
        "finance_invoice_id UUID NOT NULL",
        "finance_payment_context_id UUID NOT NULL",
        "member_id UUID NOT NULL",
        "plan_snapshot JSONB NOT NULL",
        "amount NUMERIC(14,2) NOT NULL",
        "currency_code CHAR(3) NOT NULL",
        "uq_pay4_binding_term",
        "uq_pay4_binding_invoice",
        "uq_pay4_binding_context",
        "pay4_reject_binding_mutation",
        "PAY-4 member finance binding history is immutable",
    ):
        assert token in source


def test_pay4_activation_is_finance_event_only_and_rederives_money_truth():
    source=MIGRATION.read_text(encoding="utf-8")
    body=source.split(
        "CREATE FUNCTION app_secure.apply_member_subscription_finance_event",1
    )[1].split("$function$",2)[1]
    for token in (
        "finance.outbox_events",
        "finance.invoice.paid",
        "finance.member_subscription_finance_bindings",
        "finance.payment_allocations",
        "finance.payments",
        "p.status NOT IN ('captured','settled')",
        "v_allocated IS DISTINCT FROM v_binding.amount",
        "v_invoice.grand_total_amount IS DISTINCT FROM v_binding.amount",
        "upper(v_invoice.currency_code) IS DISTINCT FROM upper(v_binding.currency_code)",
        "subscription_events",
        "event_source",
        "'finance'",
    ):
        assert token in body
    for forbidden in (
        "provider_signature",
        "provider_order_ref",
        "razorpay",
        "frontend",
        "webhook_payload",
    ):
        assert forbidden not in body.lower()


def test_pay4_runtime_cannot_bypass_activation_or_binding_tables():
    source=MIGRATION.read_text(encoding="utf-8")
    assert "trg_pay4_subscription_term_activation_guard" in source
    assert "trg_pay4_v2_activation_guard" in source
    assert "PAY-4 subscription activation requires Finance worker authority" in source
    assert "PAY-4 V2 activation requires Finance worker authority" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.apply_member_subscription_finance_event" not in source
    assert "activation capability must remain unbound until PAY-5" in source


def test_pay4_current_admission_path_is_pending_and_materializes_term():
    service=SERVICE.read_text(encoding="utf-8")
    repo=REPO.read_text(encoding="utf-8")
    checkout=CHECKOUT.read_text(encoding="utf-8")
    assert "status=ModernSubscriptionStatus.pending" in service
    assert "status=ModernSubscriptionStatus.active" not in service
    assert "app_secure.create_member_subscription_pending_term" in service
    assert "ModernSubscriptionStatus.pending" in repo
    assert "app_secure.record_member_subscription_finance_binding" in checkout


def test_pay4_downgrade_is_evidence_preserving_and_predecessor_acl_safe():
    source=MIGRATION.read_text(encoding="utf-8")
    assert "PAY-4 downgrade blocked: member-Finance binding/activation evidence exists" in source
    assert "REVOKE SELECT ON TABLE finance.invoices FROM app_security_owner" not in source
    assert "REVOKE SELECT ON TABLE finance.payments FROM app_security_owner" not in source
    assert "REVOKE SELECT ON TABLE finance.payment_allocations FROM app_security_owner" not in source
    assert "REVOKE SELECT ON TABLE finance.billing_parties FROM app_security_owner" not in source
    assert 'for table in ("invoice_lines","outbox_events")' in source


def test_pay4_machine_contract_matches_hard_gate():
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["phase"]=="PAY-4"
    assert data["inherited_pay3"]["commit"]=="4cddca720ddaac4a4f29cfc0eaf2019ecd821464"
    assert data["inherited_pay3"]["tree"]=="a4e562171d9bb3cc407063cb88bd183597318b8c"
    assert data["alembic"]=={"predecessor":"zn07d8e9f0a48","head":"zo07d8e9f0a49"}
    gate=data["activation_gate"]
    assert gate["payment_missing"]=="no_activation"
    assert gate["payment_pending"]=="no_activation"
    assert gate["payment_failed"]=="no_activation"
    assert gate["payment_amount_mismatch"]=="no_activation"
    assert gate["payment_currency_mismatch"]=="no_activation"
    assert gate["fully_applied_matching_finance_payment"]=="exactly_one_activation"
    assert data["live_money_movement"] is False
    assert data["refund_provider_execution"]=="DEFERRED_FAIL_CLOSED"
