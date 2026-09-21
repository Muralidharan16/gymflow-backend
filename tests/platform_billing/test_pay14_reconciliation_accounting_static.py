from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "zz07d8e9f0a60_pay14_reconciliation_treasury_accounting.py"
MODEL = ROOT / "app" / "platform_billing" / "models" / "accounting_reconciliation.py"
DOMAIN = ROOT / "app" / "platform_billing" / "domain" / "accounting_reconciliation.py"
REPOSITORY = ROOT / "app" / "platform_billing" / "repositories" / "accounting_reconciliation.py"
SERVICE = ROOT / "app" / "platform_billing" / "services" / "accounting_reconciliation.py"
ARCH = ROOT / "docs" / "architecture" / "PAY14_RECONCILIATION_TREASURY_ACCOUNTING.md"
CONTRACT = ROOT / "docs" / "architecture" / "pay14_reconciliation_accounting_v1.yaml"

PAY14_TABLES = {
    "platform_accounting_closure_runs",
    "platform_accounting_evidence",
    "platform_accounting_reconciliation_items",
    "platform_accounting_incidents",
}

MISMATCHES = {
    "provider_only",
    "local_only",
    "amount_mismatch",
    "currency_mismatch",
    "status_mismatch",
    "settlement_missing",
    "duplicate_provider_object",
    "unknown_provider_object",
    "refund_mismatch",
    "fee_mismatch",
}

OUTCOMES = {
    "auto_resolved_by_authoritative_evidence",
    "retry_required",
    "manual_review_required",
    "security_incident",
    "accounting_incident",
}


def test_pay14_revision_and_model_surface_are_exact():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "zz07d8e9f0a60"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "zy07d8e9f0a59"' in migration
    for table in PAY14_TABLES:
        assert f"CREATE TABLE public.{table}" in migration

    mapped = set(
        re.findall(
            r'__tablename__\s*=\s*"([^"]+)"',
            MODEL.read_text(encoding="utf-8"),
        )
    )
    assert mapped == PAY14_TABLES


def test_pay14_exact_mismatch_and_outcome_taxonomies_are_persisted():
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (MIGRATION, MODEL, DOMAIN, ARCH)
    )
    for token in MISMATCHES | OUTCOMES:
        assert token in sources


def test_pay14_automation_has_no_money_mutation_authority():
    migration = MIGRATION.read_text(encoding="utf-8")
    repository = REPOSITORY.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    domain = DOMAIN.read_text(encoding="utf-8")

    assert "automatic_financial_mutation_allowed IS FALSE" in migration
    assert "automated reconciliation may never correct money" in migration
    assert "auto_financial_mutation_allowed=False" in domain
    assert "automatic_financial_mutation_allowed=False" in repository

    forbidden_sql = (
        "UPDATE public.platform_payment_attempts",
        "UPDATE public.platform_refunds",
        "UPDATE public.platform_disputes",
        "UPDATE platform_payment_attempts",
        "UPDATE platform_refunds",
        "UPDATE platform_disputes",
        "INSERT INTO platform_dispute_financial_entries",
    )
    combined = "\n".join((migration, repository, service))
    for token in forbidden_sql:
        assert token not in combined

    forbidden_import_roots = {
        "app.platform_billing.repositories.provider_operations",
        "app.platform_billing.repositories.disputes",
        "app.finance_core",
    }
    for path in (REPOSITORY, SERVICE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not {
            item
            for item in imports
            if any(item.startswith(root) for root in forbidden_import_roots)
        }


def test_pay14_evidence_is_append_only_and_close_is_fail_closed():
    migration = MIGRATION.read_text(encoding="utf-8")
    for token in (
        "PAY-14 accounting evidence is append-only",
        "PAY-14 closure blocked by unresolved reconciliation",
        "PAY-14 closure counters must equal durable item state",
        "PAY-14 closure requires immutable evidence manifest",
        "PAY-14 closed accounting period is terminal",
    ):
        assert token in migration


def test_pay14_three_way_binding_and_authoritative_auto_resolution_are_db_enforced():
    migration = MIGRATION.read_text(encoding="utf-8")
    for token in (
        "PAY-14 local evidence binding is invalid",
        "PAY-14 provider evidence binding is invalid",
        "PAY-14 settlement evidence binding is invalid",
        "PAY-14 auto resolution requires complete authoritative evidence",
        "PAY-14 auto resolution requires authoritative evidence",
        "PAY-14 settlement side requires settlement/bank evidence",
    ):
        assert token in migration


def test_pay14_incidents_are_explicit_and_terminally_resolved():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "security_incident" in migration
    assert "accounting_incident" in migration
    assert "PAY-14 incident must bind matching reconciliation outcome" in migration
    assert "PAY-14 resolved accounting incident is terminal" in migration


def test_pay14_rls_and_runtime_are_read_only():
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


def test_pay14_contract_safety_boundaries_are_frozen():
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["predecessor_sha"] == "ad4f495f0f79b7e3624de398e6bbfc90a4f55785"
    assert contract["predecessor_tree"] == "3f481a7b163271a44f8be89c4d4fc45130d90f51"
    assert contract["revision"] == "zz07d8e9f0a60"
    assert set(contract["mismatch_categories"]) == MISMATCHES
    assert set(contract["safe_outcomes"]) == OUTCOMES
    assert contract["automatic_financial_mutation_allowed"] is False
    assert contract["authoritative_evidence_may_rewrite_money"] is False
    assert contract["close_requires_all_items_resolved"] is True
    assert contract["close_requires_all_incidents_resolved"] is True
    assert contract["member_finance_core_reused"] is False
    assert contract["live_provider"] is False
    assert contract["production_credentials"] is False
    assert contract["live_money_movement"] is False
    assert contract["production_runtime_binding"] is False
    assert contract["gate"] == "PAY14_RECONCILIATION_ACCOUNTING=PASS"
    assert "PAY14_RECONCILIATION_ACCOUNTING=PASS" in ARCH.read_text(encoding="utf-8")


def test_pay14_downgrade_fails_closed_on_any_accounting_history():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "PAY-14 downgrade blocked: reconciliation/accounting history exists" in migration
    for table in PAY14_TABLES:
        assert f"SELECT 1 FROM public.{table}" in migration
