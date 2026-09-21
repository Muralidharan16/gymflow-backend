from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "zx07d8e9f0a58_pay12_recurring_mandates_dunning.py"
RECURRING_MODEL = ROOT / "app" / "platform_billing" / "models" / "recurring.py"
PRODUCTION_MODEL = ROOT / "app" / "platform_billing" / "models" / "production.py"
DOMAIN = ROOT / "app" / "platform_billing" / "domain" / "recurring.py"
SERVICE = ROOT / "app" / "platform_billing" / "services" / "recurring_billing.py"
POLICY = ROOT / "app" / "platform_billing" / "policies" / "data" / "lifecycle_policies_v1.yaml"
ARCH = ROOT / "docs" / "architecture" / "PAY12_RECURRING_DUNNING.md"
CONTRACT = ROOT / "docs" / "architecture" / "pay12_recurring_dunning_v1.yaml"

PAY12_TABLES = {
    "platform_recurring_billing_jobs",
    "platform_dunning_cases",
    "platform_dunning_attempts",
    "platform_notification_deliveries",
}


def test_pay12_revision_and_durable_schema():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "zx07d8e9f0a58"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "zw07d8e9f0a57"' in migration
    for table in PAY12_TABLES:
        assert f"CREATE TABLE public.{table}" in migration

    model_source = RECURRING_MODEL.read_text(encoding="utf-8")
    mapped = set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', model_source))
    assert mapped == PAY12_TABLES


def test_pay12_mandate_lifecycle_and_supported_rails_are_frozen():
    migration = MIGRATION.read_text(encoding="utf-8")
    production = PRODUCTION_MODEL.read_text(encoding="utf-8")
    statuses = ("pending", "authorized", "active", "paused", "revoked", "expired", "failed")
    for status in statuses:
        assert status in migration
        assert status in production
    for rail in ("upi_autopay", "e_mandate", "card_recurring"):
        assert rail in migration
        assert rail in production
    assert "replacement_mandate_id" in migration
    assert "replacement_mandate_id" in production
    assert "terminal mandate cannot transition" in migration


def test_pay12_provider_outage_cannot_be_dunning_evidence():
    migration = MIGRATION.read_text(encoding="utf-8")
    domain = DOMAIN.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")

    assert "provider_outage_counts_as_failure: false" in POLICY.read_text(encoding="utf-8")
    assert "NON_DUNNING_EVIDENCE" in domain
    assert "ProviderOutcomeKind.RETRYABLE_FAILURE" in service
    assert "RecurringEvidenceKind.provider_outage.value" in service
    assert "provider outage evidence cannot create or advance dunning" in migration
    assert "provider_outage" not in re.search(
        r"chk_platform_dunning_cases_durable_evidence_kind.*?\),",
        migration,
        flags=re.DOTALL,
    ).group(0)


def test_pay12_policy_has_retry_grace_notification_and_recovery_rules():
    data = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    params = data["policies"]["DUNNING-IN-V1"]["params"]
    assert params["full_access_grace_days"] == 3
    assert params["limited_write_stage_days"] == 4
    assert params["read_only_stage_days"] == 7
    assert params["max_attempts"] == 4
    assert params["retry_spacing_hours"] == [0, 24, 72, 120]
    assert params["provider_outage_counts_as_failure"] is False
    assert params["final_action"] == "suspend_then_terminate"
    assert params["termination_after_suspension_days"] == 30
    assert params["late_payment_recovers_access"] is True
    assert {"upi_autopay", "e_mandate", "card_recurring"} <= set(params["supported_mandate_rails"])
    assert {
        "payment_failed",
        "retry_scheduled",
        "access_limited",
        "access_read_only",
        "subscription_suspended",
        "subscription_terminated",
        "payment_recovered",
    } <= set(params["customer_notifications"])


def test_pay12_recurring_records_are_tenant_rls_and_runtime_read_only():
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;")' in migration
    assert 'op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY;")' in migration
    assert "CREATE POLICY tenant_isolation_{table_name}" in migration
    tenant_block = migration.split("TENANT_TABLES = (", 1)[1].split(")", 1)[0]
    for table in PAY12_TABLES:
        assert f'"{table}"' in tenant_block
    assert "GRANT SELECT ON" in migration
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not re.search(
            rf"GRANT\s+{privilege}\b.*?\bTO\s+app_runtime\s*;",
            migration,
            flags=re.DOTALL,
        )


def test_pay12_has_durable_job_fences_and_terminal_fact_protection():
    migration = MIGRATION.read_text(encoding="utf-8")
    required = (
        "lease_owner",
        "lease_until",
        "lease_fence",
        "uq_platform_recurring_jobs_period",
        "terminal recurring job financial facts are immutable",
        "first confirmed failure timestamp is immutable",
        "dunning stage cannot move backward",
        "dunning recovery requires durable payment success evidence",
        "termination requires prior suspension and terminated_at",
        "terminal notification state cannot revert",
        "ux_platform_invoices_subscription_service_period",
        "issued invoice service period is immutable",
        "recurring subscription invoice requires service period before issuance",
    )
    for item in required:
        assert item in migration


def test_pay12_domain_does_not_import_member_commerce():
    forbidden = {
        "app.models.subscription",
        "app.models.payment",
        "app.models.membership_plan",
        "app.models.member_subscription_v2",
        "app.services.subscription_service",
        "app.services.payment_service",
        "app.finance_core.services.member_subscription_checkout",
    }
    for path in (DOMAIN, SERVICE, RECURRING_MODEL):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imports & forbidden)


def test_pay12_gate_contract_is_present():
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["phase"] == "PAY-12"
    assert contract["predecessor_sha"] == "a9a6a0ae46aa5003f93a956a7c40cfe456c0fad6"
    assert contract["revision"] == "zx07d8e9f0a58"
    assert contract["provider_outage_never_immediate_disable"] is True
    assert contract["time_advanced_simulation_required"] is True
    assert contract["provider_failure_injection_required"] is True
    assert contract["gate"] == "PAY12_RECURRING_DUNNING=PASS"
    assert "PAY12_RECURRING_DUNNING=PASS" in ARCH.read_text(encoding="utf-8")


def test_pay12_database_and_worker_hardening_contracts():
    migration = MIGRATION.read_text(encoding="utf-8")
    repository = (
        ROOT / "app" / "platform_billing" / "repositories" / "recurring.py"
    ).read_text(encoding="utf-8")

    for token in (
        "mandates must be created pending",
        "recurring jobs must start scheduled and unleased",
        "recurring job identity is immutable",
        "dunning identity and policy snapshot are immutable",
        "confirmed dunning attempt count must advance monotonically one at a time",
        "dunning attempts are append-only",
        "notification identity is immutable",
    ):
        assert token in migration

    assert "expired_processing" in repository
    assert "lease_owner == claim.worker_id" in repository
    assert "retry budget exhausted" in repository
    assert "candidate.max_attempts" in repository
