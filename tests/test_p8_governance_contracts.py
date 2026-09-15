from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p8_observability_matrix.json"
SCOPE_PATH = ROOT / "docs/architecture/P8_OBSERVABILITY_SCOPE_AND_GATES.md"
ACCEPTANCE_PATH = ROOT / "docs/architecture/P8_ACCEPTANCE_MATRIX.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p8-governance-observability.yml"

P7_MERGE_BASE = "c0e18944b15bee2ae7b37b9ede86a7832a232c59"
P7_CERTIFIED_TREE = "0edf7e8f997bfe8ca44af474f5f8b93b30a748ea"
P8_BRANCH = "hardening/p8-observability-operational-response"

FINAL_MARKERS = {
    "P8_GOVERNANCE_OBSERVABILITY=PASS",
    "P8_STRUCTURED_LOG_CONTEXT=PASS",
    "P8_SENSITIVE_REDACTION=PASS",
    "P8_METRIC_CARDINALITY_BOUNDED=PASS",
    "P8_API_METRICS=PASS",
    "P8_DATABASE_METRICS=PASS",
    "P8_QUEUE_METRICS=PASS",
    "P8_LIFECYCLE_METRICS=PASS",
    "P8_FINANCE_METRICS=PASS",
    "P8_PROVIDER_METRICS=PASS",
    "P8_PLATFORM_METRICS=PASS",
    "P8_CRITICAL_ALERT_COVERAGE=PASS",
    "P8_SLO_CONTRACT=PASS",
    "P8_RUNBOOK_COVERAGE=PASS",
    "P8_PRODUCTION_LIKE_OBSERVABILITY=PASS",
    "P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS",
    "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    "P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS",
    "P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS",
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p8_binds_exact_post_merge_certified_p7_baseline() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P8"
    assert matrix["base_commit"] == P7_MERGE_BASE
    assert matrix["base_tree"] == P7_CERTIFIED_TREE
    assert matrix["branch"] == P8_BRANCH
    assert matrix["slices"] == ["P8-G", "P8-L", "P8-M", "P8-A", "P8-R", "P8-O", "P8-F"]
    assert matrix["hard_gate"] == "every_critical_failure_mode_visible_and_actionable"


def test_p8_freezes_structured_context_and_metric_cardinality_boundary() -> None:
    context = _matrix()["structured_context"]
    assert set(context["logs_and_traces"]) == {
        "request_id",
        "correlation_id",
        "tenant_id",
        "branch_id",
        "principal_id",
        "principal_type",
        "saga_id",
        "task_id",
        "trace_id",
        "span_id",
    }
    assert set(context["metrics_high_cardinality_labels_forbidden"]) == {
        "request_id",
        "correlation_id",
        "principal_id",
        "saga_id",
        "task_id",
        "trace_id",
        "span_id",
    }
    assert context["metrics_tenant_or_branch_labels_default_forbidden"] is True
    assert context["unknown_or_not_applicable_values_must_be_explicit"] is True


def test_p8_freezes_recursive_redaction_before_any_sink() -> None:
    redaction = _matrix()["redaction"]
    assert redaction["recursive_before_sink"] is True
    assert redaction["authorization_headers"] is True
    assert redaction["cookies"] is True
    assert redaction["access_and_refresh_tokens"] is True
    assert redaction["passwords_and_secrets"] is True
    assert redaction["api_keys"] is True
    assert redaction["webhook_signatures"] is True
    assert redaction["database_credentials"] is True
    assert redaction["provider_credentials"] is True
    assert redaction["financial_credentials"] is True
    assert redaction["granular_location_pii"] is True
    assert redaction["email_phone_and_government_identifiers"] is True
    assert redaction["exception_locals_and_breadcrumbs"] is True
    assert redaction["redaction_marker"] == "[REDACTED]"


def test_p8_metric_domains_cover_all_required_operational_surfaces() -> None:
    domains = _matrix()["metric_domains"]
    assert set(domains) == {"api", "database", "queues", "lifecycle", "finance", "providers", "platform"}
    assert {"requests", "errors", "latency", "inflight", "readiness", "drain_rejections"} <= set(domains["api"])
    assert {"pool_utilization", "pool_wait", "pool_timeouts", "disconnects"} <= set(domains["database"])
    assert {"depth", "oldest_message_age", "redeliveries", "dead_letters", "worker_availability"} <= set(domains["queues"])
    assert {"pending", "stuck", "failed", "replayed", "compensations"} <= set(domains["lifecycle"])
    assert {"reconciliation_mismatches", "provider_ack_ambiguity", "refund_obligations"} <= set(domains["finance"])
    assert {"errors", "latency", "timeouts", "rate_limits", "circuit_open"} <= set(domains["providers"])
    assert {"redis_health", "scheduler_ownership", "backup_age", "backup_failures"} <= set(domains["platform"])


def test_p8_every_frozen_critical_failure_requires_alert_and_runbook() -> None:
    failures = _matrix()["critical_failure_modes"]
    expected = {
        "dead_letter_present",
        "stuck_saga_or_lifecycle",
        "queue_oldest_age_breach",
        "finance_reconciliation_mismatch",
        "provider_success_db_ack_ambiguity",
        "database_pool_exhaustion",
        "database_disconnect_storm",
        "redis_or_broker_unavailable",
        "worker_unavailable_or_redelivery_storm",
        "duplicate_scheduler_effect_risk",
        "backup_failure_or_stale_backup",
        "api_availability_or_latency_slo_breach",
        "provider_error_or_timeout_slo_breach",
        "observability_pipeline_failure",
    }
    assert set(failures) == expected
    for definition in failures.values():
        assert definition == {"severity": "critical", "must_alert": True, "runbook_required": True}


def test_p8_freezes_alert_actionability_and_slos() -> None:
    actionability = _matrix()["alert_actionability"]
    assert all(actionability.values())
    slos = _matrix()["slo_contract"]
    assert all(slos.values())


def test_p8_decisive_evidence_cannot_be_mock_only() -> None:
    evidence = _matrix()["decisive_evidence"]
    assert all(evidence.values())


def test_p8_preserves_business_authority_and_hard_stops() -> None:
    matrix = _matrix()
    assert matrix["authority"] == {
        "durable_business_work": "postgresql",
        "redis_celery_beat": "delivery_coordination_only",
        "refund_provider_execution": "deferred_fail_closed",
        "observability_data_is_not_business_authority": True,
    }
    assert set(matrix["hard_stops"]) == {
        "raw_secret_or_sensitive_pii_in_logs_traces_errors_or_metrics",
        "high_cardinality_identifiers_as_metrics_labels",
        "observability_sink_failure_breaks_business_authority",
        "public_unauthenticated_metrics_endpoint_without_network_boundary",
        "p1_p7_security_or_runtime_semantic_weakening",
        "database_credential_broadening",
        "refund_provider_activation",
        "live_money_movement",
        "release",
        "deployment",
    }


def test_p8_acceptance_locks_terminal_markers_and_key_language() -> None:
    acceptance = ACCEPTANCE_PATH.read_text(encoding="utf-8")
    scope = " ".join(SCOPE_PATH.read_text(encoding="utf-8").split())
    for marker in FINAL_MARKERS:
        assert marker in acceptance
    for phrase in (
        "Observability data is evidence, not business truth",
        "forbidden as metrics labels",
        "Redaction must happen before data reaches any log, trace, error-reporting or telemetry sink",
        "Public unauthenticated Internet exposure is forbidden",
        "Mock-only observability proof is forbidden",
        "every critical failure mode is visible and actionable on the exact same immutable head",
    ):
        assert phrase in scope


def test_p8_governance_workflow_is_exact_base_and_branch_bound() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P8_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"governance"}
    job = workflow["jobs"]["governance"]
    assert job["env"]["P7_MERGE_BASE"] == P7_MERGE_BASE
    assert job["env"]["P7_CERTIFIED_TREE"] == P7_CERTIFIED_TREE
    assert "git merge-base --is-ancestor \"${P7_MERGE_BASE}\" HEAD" in source
    assert "tests/test_p8_governance_contracts.py" in source
    assert "tests/test_p7f_final_certification_contracts.py" in source
    assert "P8_GOVERNANCE_OBSERVABILITY=PASS" in source
    assert "P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=GOVERNANCE_FROZEN" in source
    assert "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source
