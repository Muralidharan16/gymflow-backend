from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/zz27d8e9f0a62_pay16_security_abuse_hardening.py"


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pay16_migration_is_single_head_and_frozen_to_pay15():
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "zz27d8e9f0a62"' in source
    assert 'down_revision = "zz17d8e9f0a61"' in source
    assert "PAY-16 migration requires migration_owner" in source
    assert "app_runtime can reach app_security_owner" in source


def test_security_audit_is_force_rls_append_only_and_hash_chained():
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "CREATE TABLE finance.security_audit_events",
        "sequence_no BIGINT NOT NULL",
        "previous_event_hash CHAR(64) NULL",
        "event_hash CHAR(64) NOT NULL",
        "UNIQUE(organization_id,sequence_no)",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "pg_advisory_xact_lock",
        "hashtextextended",
        "jsonb_build_object",
        "sha256",
        "trg_pay16_security_audit_immutable",
        "BEFORE UPDATE OR DELETE OR TRUNCATE",
        "PAY-16 Finance security audit is append-only",
    ):
        assert required in source


def test_runtime_has_only_exact_append_capability_not_table_dml():
    source = MIGRATION.read_text(encoding="utf-8")
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.record_finance_security_audit("
        in source
    )
    assert "TO app_runtime" in source
    assert "REVOKE ALL ON TABLE finance.security_audit_events FROM PUBLIC" in source
    assert "has_table_privilege" in source
    assert "leaked direct app_runtime Finance security-audit" in source
    assert "GRANT SELECT,INSERT" in source
    assert "TO app_security_owner" in source


def test_security_audit_derives_identity_from_database_context():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "app.current_org_id" in source
    assert "app.current_user_id" in source
    assert "app.current_role" in source
    assert "app.request_id" in source
    assert "v_role NOT IN ('owner','admin')" in source
    assert "p_organization_id" not in source
    assert "p_actor_id" not in source


def test_downgrade_is_empty_only_and_preserves_security_evidence():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "PAY-16 downgrade blocked: Finance security audit evidence exists" in source
    assert "NO FORCE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert source.index("if has_evidence:") < source.index(
        "DROP TABLE finance.security_audit_events"
    )


def test_checkout_and_offline_money_are_bound_to_step_up_and_durable_audit():
    checkout = _source("app/finance_core/api/payment_boundary.py")
    offline = _source("app/finance_core/api/offline_payments.py")

    assert "finance_high_risk_actor_dependency" in checkout
    assert 'event_type="finance.security.checkout.initiated"' in checkout
    assert checkout.index("FinanceSecurityAuditService(db).record") < checkout.index(
        "# Durable local authority exists before any provider I/O."
    )

    for event in (
        "finance.security.offline_payment.prepared",
        "finance.security.offline_payment.approved",
        "finance.security.offline_payment.rejected",
    ):
        assert event in offline
    assert "X-Finance-Reason-Code" in offline


def test_security_metrics_are_bounded_and_alert_rules_are_actionable():
    metrics = _source("app/observability/runtime_metrics.py")
    security = _source("app/finance_core/security_abuse.py")
    rules = yaml.safe_load(
        _source("ops/observability/pay16_security_rules.yml")
    )
    contract = json.loads(
        _source("docs/architecture/pay16_security_alert_contract_v1.json")
    )

    assert 'meter.create_counter(\n            "doers.finance.security_events"' in metrics
    assert "_FINANCE_SECURITY_EVENTS = frozenset" in metrics
    assert "_SECURITY_SEVERITIES = frozenset" in metrics
    assert "runtime_metrics().finance_security_event" in security

    alerts = {
        rule["alert"]
        for group in rules["groups"]
        for rule in group["rules"]
    }
    assert alerts == {
        "DoersFinanceSecurityAbuseSpike",
        "DoersFinanceMakerCheckerViolation",
        "DoersFinanceSecurityVerifierUnavailable",
    }
    assert contract["prometheus_metric"] == "doers_finance_security_events_total"
    assert set(contract["alerts"]) == {
        "finance_security_abuse_spike",
        "maker_checker_violation",
        "revocation_verifier_unavailable",
    }
    for definition in contract["alerts"].values():
        assert definition["runbook"] == "docs/runbooks/pay16/finance-security-abuse.md"


def test_security_runbook_forbids_dangerous_operator_shortcuts():
    runbook = _source("docs/runbooks/pay16/finance-security-abuse.md").lower()
    for required in (
        "do not manually update finance status columns",
        "bypass maker-checker",
        "replay provider mutations",
        "do not weaken the pay-16 fail-closed dependency",
        "immutable audit chain",
    ):
        assert required in runbook
