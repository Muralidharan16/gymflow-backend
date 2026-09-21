from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "zy07d8e9f0a59_pay13_disputes_financial_exceptions.py"
MODEL = ROOT / "app" / "platform_billing" / "models" / "disputes.py"
DOMAIN = ROOT / "app" / "platform_billing" / "domain" / "disputes.py"
REPOSITORY = ROOT / "app" / "platform_billing" / "repositories" / "disputes.py"
SERVICE = ROOT / "app" / "platform_billing" / "services" / "disputes.py"
ARCH = ROOT / "docs" / "architecture" / "PAY13_DISPUTES_FINANCIAL_EXCEPTIONS.md"
CONTRACT = ROOT / "docs" / "architecture" / "pay13_dispute_exception_handling_v1.yaml"

PAY13_TABLES = {
    "platform_disputes",
    "platform_dispute_evidence",
    "platform_dispute_events",
    "platform_dispute_financial_entries",
    "platform_financial_exception_cases",
}


def test_pay13_revision_and_model_surface_are_exact():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "zy07d8e9f0a59"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "zx07d8e9f0a58"' in migration
    for table in PAY13_TABLES:
        assert f"CREATE TABLE public.{table}" in migration

    model_source = MODEL.read_text(encoding="utf-8")
    mapped = set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', model_source))
    assert mapped == PAY13_TABLES


def test_payment_truth_is_never_rewritten_by_dispute_domain():
    domain = DOMAIN.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "preserve_payment_status: bool = True" in domain
    assert "payment_status_rewrite: str | None = None" in service
    assert "captured payment truth is immutable after dispute" in migration
    assert "historically captured payment" in migration


def test_dispute_state_liability_and_crash_atomicity_contract():
    migration = MIGRATION.read_text(encoding="utf-8")
    for state in (
        "opened",
        "evidence_required",
        "submitted",
        "under_review",
        "won",
        "lost",
        "closed",
    ):
        assert state in migration
    for token in (
        "opened dispute requires liability entry in same transaction",
        "won dispute requires liability reversal in same transaction",
        "lost dispute requires loss recognition in same transaction",
        "DEFERRABLE INITIALLY DEFERRED",
        "dispute financial entries are append-only",
        "dispute evidence is append-only",
        "dispute events are append-only",
    ):
        assert token in migration


def test_active_dispute_freezes_new_mutable_money_actions():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "active dispute freezes new payment attempts on disputed invoice" in migration
    assert "active dispute freezes new refund requests" in migration
    assert "active dispute freezes refund execution" in migration
    assert "trg_platform_payment_attempts_pay13_dispute_guard" in migration
    assert "trg_platform_refunds_pay13_dispute_guard" in migration


def test_exception_types_fail_closed_to_manual_review():
    migration = MIGRATION.read_text(encoding="utf-8")
    domain = DOMAIN.read_text(encoding="utf-8")
    for kind in (
        "accidental_duplicate_provider_payment",
        "orphan_provider_payment",
        "orphan_settlement",
        "unknown_refund",
        "wrong_customer_mapping",
        "unmapped_dispute",
    ):
        assert kind in migration
    assert "manual_review_required IS TRUE" in migration
    assert "automatic_financial_mutation_allowed IS FALSE" in migration
    assert "automatic_financial_mutation_allowed=False" in domain


def test_chargeback_reversal_is_append_only_loss_reversal():
    domain = DOMAIN.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'loss_reversed = "loss_reversed"' in domain
    assert "chargeback reversal requires recorded dispute loss" in migration
    assert "loss_reversed" in migration


def test_pay13_rls_and_runtime_authority_are_fail_closed():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;")' in migration
    assert 'op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY;")' in migration
    assert "GRANT SELECT ON" in migration
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not re.search(
            rf"GRANT\s+{privilege}\b.*?\bTO\s+app_runtime\s*;",
            migration,
            flags=re.DOTALL,
        )


def test_pay13_domain_does_not_import_member_commerce():
    forbidden = {
        "app.models.subscription",
        "app.models.payment",
        "app.models.membership_plan",
        "app.models.member_subscription_v2",
        "app.services.subscription_service",
        "app.services.payment_service",
        "app.finance_core.services.member_subscription_checkout",
    }
    for path in (DOMAIN, MODEL, REPOSITORY, SERVICE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imports & forbidden)


def test_pay13_contract_and_safety_boundaries():
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["predecessor_sha"] == "60549fcb5bee4649d229e8e99b056d6d378f479a"
    assert contract["predecessor_tree"] == "dbf5cb9cc59a83e8d2d36ebe1e3ac29371b497e2"
    assert contract["revision"] == "zy07d8e9f0a59"
    assert contract["captured_payment_truth_immutable"] is True
    assert contract["dispute_liability_required_same_transaction"] is True
    assert contract["provider_decision_financial_closure_required_same_transaction"] is True
    assert contract["financial_exceptions_automatic_money_mutation"] is False
    assert contract["live_provider"] is False
    assert contract["live_money_movement"] is False
    assert contract["production_runtime_binding"] is False
    assert contract["gate"] == "PAY13_DISPUTE_EXCEPTION_HANDLING=PASS"
    assert "PAY13_DISPUTE_EXCEPTION_HANDLING=PASS" in ARCH.read_text(encoding="utf-8")


def test_pay13_downgrade_fails_closed_on_financial_history():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "PAY-13 downgrade blocked: dispute/exception financial history exists" in migration
    for table in PAY13_TABLES:
        assert f"SELECT 1 FROM public.{table}" in migration
