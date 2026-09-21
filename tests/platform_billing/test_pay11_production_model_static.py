from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "zw07d8e9f0a57_pay11_platform_billing_production_model.py"
MODEL = ROOT / "app" / "platform_billing" / "models" / "production.py"
SUBSCRIPTION_MODEL = ROOT / "app" / "platform_billing" / "models" / "subscription.py"
CATALOG_MANIFEST = ROOT / "app" / "platform_billing" / "policies" / "data" / "catalog_release_v1.json"
PROVIDER_MANIFEST = ROOT / "app" / "platform_billing" / "policies" / "data" / "provider_release_v1.json"
ARCH = ROOT / "docs" / "architecture" / "PAY11_PLATFORM_BILLING_PRODUCTION_MODEL.md"
CONTRACT = ROOT / "docs" / "architecture" / "pay11_platform_billing_model_v1.json"

REQUIRED_ENTITIES = {
    "platform_provider_subscriptions",
    "platform_mandates",
    "platform_document_sequences",
    "platform_invoices",
    "platform_invoice_lines",
    "platform_payment_attempts",
    "platform_refunds",
    "platform_credit_notes",
    "platform_credit_note_lines",
}

RELEASE_TABLES = {
    "platform_catalog_releases",
    "platform_catalog_release_items",
    "platform_provider_releases",
}


def _model_tables() -> set[str]:
    source = MODEL.read_text(encoding="utf-8")
    return set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', source))


def test_pay11_exact_revision_and_required_schema_surface():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "zw07d8e9f0a57"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "zv07d8e9f0a56"' in migration
    for table in REQUIRED_ENTITIES | RELEASE_TABLES:
        assert f"CREATE TABLE public.{table}" in migration

    mapped = _model_tables()
    assert REQUIRED_ENTITIES | RELEASE_TABLES <= mapped


def test_pay11_commercial_release_manifests_are_versioned_non_live_contracts():
    catalog = json.loads(CATALOG_MANIFEST.read_text(encoding="utf-8"))
    provider = json.loads(PROVIDER_MANIFEST.read_text(encoding="utf-8"))

    assert catalog["manifest_id"] == "catalog_release_v1"
    assert catalog["schema_version"] == 1
    assert catalog["publication_state"] == "draft"
    assert catalog["live_activation"] is False
    assert catalog["immutable_after_publication"] is True
    assert catalog["requires_controlled_subscription_migration"] is True

    assert provider["manifest_id"] == "provider_release_v1"
    assert provider["schema_version"] == 1
    assert provider["publication_state"] == "draft"
    assert provider["live_activation"] is False
    assert provider["production_credentials_allowed"] is False
    assert provider["immutable_after_publication"] is True
    assert provider["provider_code"] == "unbound"


def test_pay11_subscription_has_explicit_accepted_contract_binding():
    source = SUBSCRIPTION_MODEL.read_text(encoding="utf-8")
    for field in (
        "accepted_catalog_release_id",
        "accepted_provider_release_id",
        "accepted_plan_version_id",
        "accepted_price_id",
        "commercial_contract_sha256",
        "commercial_contract_accepted_at",
        "commercial_contract_migrated_at",
        "commercial_contract_migration_reason",
    ):
        assert field in source

    migration = MIGRATION.read_text(encoding="utf-8")
    assert "trg_platform_subscriptions_commercial_contract" in migration
    assert "migrate_platform_subscription_commercial_contract" in migration
    assert "accepted commercial contract cannot drift outside controlled migration" in migration
    assert "platform.subscription.commercial_contract_migrated" in migration


def test_pay11_issued_documents_and_refund_capacity_are_database_enforced():
    migration = MIGRATION.read_text(encoding="utf-8")
    required = (
        "trg_platform_invoice_lines_immutable_after_issue",
        "trg_platform_invoices_legal_immutability",
        "invoice header totals must equal immutable line snapshot totals",
        "trg_platform_credit_note_lines_immutable_after_issue",
        "trg_platform_credit_notes_legal_immutability",
        "cumulative credit notes cannot exceed invoice total",
        "trg_platform_refunds_validate",
        "cumulative refunds cannot exceed captured payment",
        "succeeded refund requires credit note and authoritative provider evidence",
        "terminal payment fact cannot revert",
        "payment attempt financial identity is immutable",
        "terminal refund fact cannot revert",
        "refund financial identity is immutable",
    )
    for phrase in required:
        assert phrase in migration


def test_pay11_runtime_authority_is_read_only_and_document_sequence_is_internal():
    migration = MIGRATION.read_text(encoding="utf-8")
    marker = "GRANT SELECT ON"
    assert marker in migration
    runtime_block = migration.split(marker, 1)[1].split("TO app_runtime;", 1)[0]
    assert "platform_document_sequences" not in runtime_block

    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not re.search(
            rf"GRANT\\s+{privilege}\\b.*?\\bTO\\s+app_runtime\\s*;",
            migration,
            flags=re.DOTALL,
        )


def test_pay11_keeps_platform_billing_out_of_member_commerce_modules():
    tree = ast.parse(MODEL.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {
        "app.models.subscription",
        "app.models.payment",
        "app.models.membership_plan",
        "app.models.member_subscription_v2",
        "app.services.payment_service",
        "app.services.invoice_service",
    }
    assert not (imported & forbidden)


def test_pay11_machine_contract_and_gate_are_frozen():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert contract["predecessor_sha"] == "4fbbde1f4fe809f433f7a4b19894c59dcaffed9b"
    assert contract["predecessor_tree"] == "ac51aff5b1c68dbec655744834ea3a070dd81ff3"
    assert contract["revision"] == "zw07d8e9f0a57"
    assert set(contract["required_entities"]) == REQUIRED_ENTITIES
    assert contract["plan_price_immutable_after_publication"] is True
    assert contract["existing_subscriber_contract_retention"] is True
    assert contract["runtime_new_financial_dml"] is False
    assert contract["live_provider"] is False
    assert contract["live_money_movement"] is False
    assert contract["gate"] == "PAY11_PLATFORM_BILLING_MODEL=PASS"
    assert "PAY11_PLATFORM_BILLING_MODEL=PASS" in ARCH.read_text(encoding="utf-8")


def test_pay11_downgrade_guard_bypasses_force_rls_only_inside_migration_transaction():
    migration = MIGRATION.read_text(encoding="utf-8")
    downgrade = migration.split("def downgrade() -> None:", 1)[1]
    guard_prefix = downgrade.split("DO $pay11_downgrade_guard$", 1)[0]
    assert "NO FORCE ROW LEVEL SECURITY" in guard_prefix
    assert "PAY-11 downgrade blocked" in downgrade
