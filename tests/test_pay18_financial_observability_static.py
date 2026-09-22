from __future__ import annotations

import json
import re
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.finance_core.observability import sanitized_finance_correlation
from app.observability.context import (
    bind_observability_context,
    reset_observability_context,
)
from app.observability.runtime_metrics import (
    FORBIDDEN_METRIC_ATTRIBUTE_KEYS,
    RuntimeMetrics,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/architecture/pay18_financial_observability_contract_v1.json"
MIGRATION = ROOT / "alembic/versions/zz37d8e9f0a63_pay18_financial_observability.py"
RULES = ROOT / "ops/observability/pay18_financial_rules.yml"
RUNTIME = ROOT / "app/observability/runtime_metrics.py"
TASK = ROOT / "app/tasks/external_effect_observability.py"
WEBHOOK = ROOT / "app/finance_core/services/razorpay_webhooks.py"

REQUIRED_METRICS = {
    "payment_attempt_total",
    "payment_failure_total",
    "payment_unknown_total",
    "webhook_signature_failure_total",
    "webhook_backlog",
    "payment_application_backlog",
    "finance_outbox_backlog",
    "refund_backlog",
    "refund_unknown_total",
    "settlement_mismatch_total",
    "reconciliation_open_total",
    "mandate_failure_total",
    "dunning_stage_total",
    "chargeback_open_total",
}
REQUIRED_INCIDENTS = {
    "provider_outage",
    "webhook_outage",
    "unknown_payment",
    "duplicate_payment_allegation",
    "settlement_mismatch",
    "refund_ambiguity",
    "chargeback",
    "expired_mandate",
    "queue_backlog",
    "database_recovery",
    "credential_compromise",
}


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _observed(metrics_data) -> dict[str, object]:
    result = {}
    for resource in metrics_data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                result[metric.name] = metric
    return result


def test_pay18_is_bound_to_frozen_pay17_and_new_migration_head() -> None:
    contract = _contract()
    assert contract["phase"] == "PAY-18"
    assert contract["predecessor"] == {
        "phase": "PAY-17",
        "sha": "ec165905756d625117f3cd5e9e4437fbab36dbb5",
        "tree": "fae1abba458aa03c3e0923ce76c6eafe8e1e2694",
        "alembic_head": "zz27d8e9f0a62",
    }
    assert contract["alembic_head"] == "zz37d8e9f0a63"
    assert contract["terminal_marker"] == "PAY18_FINANCIAL_OBSERVABILITY=PASS"


def test_required_metric_contract_is_complete_and_identity_labels_are_forbidden() -> None:
    contract = _contract()
    assert set(contract["metrics"]) == REQUIRED_METRICS
    assert "duplicate_payment_allegation_open_total" in contract["supplemental_metrics"]

    forbidden = set(contract["forbidden_metric_labels"])
    for token in (
        "payment_id",
        "customer_id",
        "organization_id",
        "tenant_id",
        "provider_payment_ref",
        "request_id",
        "correlation_id",
        "amount",
        "currency",
        "signature",
        "payload",
    ):
        assert token in forbidden

    for definition in (
        list(contract["metrics"].values())
        + list(contract["supplemental_metrics"].values())
    ):
        assert not (set(definition["labels"]) & forbidden)
        assert definition["source"]

    # P8's global cardinality policy remains inherited and PAY-18 tightens it.
    assert {
        "request_id",
        "correlation_id",
        "tenant_id",
        "principal_id",
    } <= FORBIDDEN_METRIC_ATTRIBUTE_KEYS


def test_real_sdk_records_pay18_metrics_with_only_bounded_dimensions() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = RuntimeMetrics(provider.get_meter("pay18-certification", "1.0"))

    recorder.financial_observability_snapshot(
        payment_attempt_total=11,
        payment_failure_total=2,
        payment_unknown_total=1,
        webhook_backlog=3,
        payment_application_backlog=4,
        finance_outbox_backlog=5,
        refund_backlog=6,
        refund_unknown_total=1,
        settlement_mismatch_total=1,
        reconciliation_open_total=2,
        mandate_failed_total=1,
        mandate_expired_total=2,
        mandate_revoked_total=3,
        dunning_full_grace_total=4,
        dunning_limited_write_total=3,
        dunning_read_only_total=2,
        dunning_billing_only_total=1,
        dunning_recovered_total=5,
        chargeback_open_total=1,
        duplicate_payment_allegation_open_total=1,
    )
    recorder.webhook_signature_failure(provider="razorpay", reason="invalid")

    observed = _observed(reader.get_metrics_data())
    expected_otel = REQUIRED_METRICS - {"webhook_signature_failure_total"}
    expected_otel.add("webhook_signature_failure")
    assert expected_otel <= set(observed)
    assert "duplicate_payment_allegation_open_total" in observed

    for name in expected_otel | {"duplicate_payment_allegation_open_total"}:
        for point in observed[name].data.data_points:
            attrs = dict(point.attributes)
            assert not (set(attrs) & _contract()["forbidden_metric_labels"])
            assert set(attrs) <= {"provider", "reason", "state", "stage"}

    provider.shutdown()


def test_finance_correlation_is_hmac_sanitized_and_never_raw() -> None:
    tokens = bind_observability_context(
        request_id="req-pay18-raw",
        correlation_id="corr-pay18-raw",
    )
    try:
        first = sanitized_finance_correlation()
        second = sanitized_finance_correlation()
    finally:
        reset_observability_context(tokens)

    assert first == second
    assert re.fullmatch(r"fin-[0-9a-f]{20}", first)
    assert "corr-pay18-raw" not in first
    assert "req-pay18-raw" not in first

    helper = (ROOT / "app/finance_core/observability.py").read_text(encoding="utf-8")
    assert "hmac.new(" in helper
    assert "hashlib.sha256" in helper
    assert "return f\"fin-{digest[:20]}\"" in helper


def test_webhook_signature_failure_logging_contains_no_sensitive_provider_evidence() -> None:
    source = WEBHOOK.read_text(encoding="utf-8")
    assert 'webhook_signature_failure(' in source
    assert 'reason="missing"' in source
    assert 'reason="invalid"' in source
    assert '"finance_correlation": sanitized_finance_correlation()' in source

    # The warning extra is intentionally categorical. Raw signature/body/ref
    # values may be used for verification/normalization but never log fields.
    warning_blocks = re.findall(
        r'logger\.warning\((.*?)\n\s*\)',
        source,
        flags=re.DOTALL,
    )
    combined = "\n".join(warning_blocks)
    for forbidden in (
        "webhook.signature",
        "raw_body",
        "provider_payment_id",
        "provider_order_id",
        "payment_id",
        "customer_id",
    ):
        assert forbidden not in combined


def test_aggregate_snapshot_is_security_definer_read_only_and_reduced_role_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    start = source.index(
        "CREATE FUNCTION app_secure.pay18_financial_observability_snapshot()"
    )
    opening = source.index("$function$", start)
    closing = source.index("$function$", opening + len("$function$"))
    function = source[start : closing + len("$function$")]
    assert "SECURITY DEFINER" in function
    assert "LANGUAGE sql" in function
    assert "STABLE" in function
    assert "SET row_security=on" in function
    assert "INSERT " not in function
    assert "UPDATE " not in function
    assert "DELETE " not in function
    assert "TRUNCATE " not in function
    assert "RETURNS TABLE(" in function
    assert "_id uuid" not in function.lower()
    assert "provider_ref" not in function.lower()
    assert "customer" not in function.lower()

    assert "TO lifecycle_maintenance_runtime" in source
    assert "PAY-18 unexpected snapshot EXECUTE for" in source
    assert "PAY-18 maintenance runtime leaked direct SELECT" in source
    assert "TO app_security_owner" in source
    assert "USING (true)" in source


def test_scheduled_snapshot_wires_postgresql_truth_to_pay18_metrics() -> None:
    task = TASK.read_text(encoding="utf-8")
    assert "app_secure.pay18_financial_observability_snapshot()" in task
    assert "metrics.financial_observability_snapshot(" in task
    assert '"finance.observability.snapshot"' in task
    assert "sanitized_finance_correlation()" in task
    assert "platform_refund_backlog" in task
    assert "reconciliation_pending_count" in task

    runtime = RUNTIME.read_text(encoding="utf-8")
    for metric in REQUIRED_METRICS - {"webhook_signature_failure_total"}:
        assert f'"{metric}"' in runtime
    assert '"webhook_signature_failure"' in runtime


def test_alerts_and_runbooks_cover_every_required_financial_incident() -> None:
    contract = _contract()
    assert set(contract["incidents"]) == REQUIRED_INCIDENTS

    rules = RULES.read_text(encoding="utf-8")
    paths = []
    for incident, definition in contract["incidents"].items():
        path = ROOT / definition["runbook"]
        assert path.is_file(), incident
        paths.append(definition["runbook"])
        assert definition["runbook"] in rules
        assert f"failure_mode: {incident}" in rules

        text = path.read_text(encoding="utf-8")
        for heading in contract["required_runbook_sections"]:
            assert f"## {heading}" in text, (incident, heading)

        diagnosis = text.split("## Diagnosis", 1)[1].split("## Safe first actions", 1)[0]
        assert len(re.findall(r"^\d+\. ", diagnosis, flags=re.MULTILINE)) >= 5

        safe = text.split("## Safe first actions", 1)[1].split("## Forbidden actions", 1)[0]
        assert len(re.findall(r"^- ", safe, flags=re.MULTILINE)) >= 2

        forbidden = text.split("## Forbidden actions", 1)[1].split("## Escalation", 1)[0]
        lines = re.findall(r"^- (.+)$", forbidden, flags=re.MULTILINE)
        assert len(lines) >= 2
        assert all("never" in line.lower() for line in lines)

        assert "PAY18_" not in text or "does not authorize" in text.lower()

    assert len(paths) == len(set(paths)) == 11


def test_alert_labels_are_bounded_and_never_entity_identity() -> None:
    rules = RULES.read_text(encoding="utf-8")
    forbidden_label_keys = {
        "payment_id",
        "customer_id",
        "organization_id",
        "tenant_id",
        "invoice_id",
        "refund_id",
        "dispute_id",
        "request_id",
        "correlation_id",
    }
    for key in forbidden_label_keys:
        assert f"{key}:" not in rules
        assert "{" + key + "=" not in rules

    assert "payment_unknown_total" in rules
    assert "webhook_backlog" in rules
    assert "settlement_mismatch_total" in rules
    assert "refund_unknown_total" in rules
    assert "chargeback_open_total" in rules
    assert 'mandate_failure_total{state="expired"}' in rules
    assert "webhook_signature_failure_total" in rules


def test_pay18_preserves_pay16_tamper_evident_audit_and_locked_safety() -> None:
    contract = _contract()
    assert contract["audit"]["privileged_finance_actions"].startswith(
        "finance.security_audit_events"
    )
    assert contract["audit"]["immutable_chain"] is True
    assert contract["audit"]["observability_may_not_mutate_business_state"] is True
    assert contract["safety"] == {
        "live_provider": "disabled",
        "production_credentials": "disabled",
        "live_money_movement": "disabled",
        "production_runtime_binding": "disabled",
        "merge": "not_authorized",
        "release": "not_authorized",
        "deployment": "not_authorized",
    }
