from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_PATH = ROOT / "docs/architecture/p8_observability_matrix.json"
METRIC_CONTRACT_PATH = ROOT / "docs/architecture/p8_metric_contract.json"
ALERT_CONTRACT_PATH = ROOT / "docs/architecture/p8_alert_slo_contract.json"
RULES_PATH = ROOT / "ops/observability/p8_rules.yml"
DOC_PATH = ROOT / "docs/architecture/P8A_ALERTS_AND_SLOS.md"
RUNTIME_METRICS_SOURCE = (ROOT / "app/observability/runtime_metrics.py").read_text(encoding="utf-8")
METRICS_BOOTSTRAP_SOURCE = (ROOT / "app/observability/metrics_bootstrap.py").read_text(encoding="utf-8")

P8M_CERTIFIED_HEAD = "5ef26cd1698e12589b586596b60641bbeba507bd"
P8M_CERTIFIED_TREE = "4c6eb08ba4b93352c45ee302910f37c2d37d2006"

EXPECTED_SLOS = {
    "api_availability",
    "api_latency",
    "durable_queue_freshness",
    "lifecycle_completion",
    "finance_reconciliation",
    "provider_success_rate",
    "backup_freshness",
}

REQUIRED_ALERT_FIELDS = {
    "alert",
    "owner",
    "severity",
    "query",
    "threshold",
    "sustain_window",
    "deduplication_key",
    "customer_impact",
    "runbook",
    "safe_first_actions",
    "escalation_condition",
}

FORBIDDEN_LABEL_KEYS = {
    "request_id",
    "correlation_id",
    "tenant_id",
    "branch_id",
    "principal_id",
    "saga_id",
    "task_id",
    "trace_id",
    "span_id",
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def _recording_rules() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for group in _rules()["groups"]:
        for rule in group["rules"]:
            if "record" in rule:
                result[rule["record"]] = rule
    return result


def _alert_rules() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for group in _rules()["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                result[rule["alert"]] = rule
    return result


def test_p8a_is_exactly_bound_to_certified_p8m_parent() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    assert contract["schema_version"] == 1
    assert contract["phase"] == "P8-A"
    assert contract["parent_certified_head"] == P8M_CERTIFIED_HEAD
    assert contract["parent_certified_tree"] == P8M_CERTIFIED_TREE
    assert contract["rule_backend"]["dialect"] == "promql"
    assert contract["rule_backend"]["evaluation_interval_seconds"] == 30
    assert contract["rule_backend"]["public_rule_or_metrics_endpoint"] is False
    assert contract["rule_backend"]["observability_is_business_authority"] is False


def test_every_frozen_critical_failure_mode_has_exactly_one_actionable_alert_contract() -> None:
    governance = _json(GOVERNANCE_PATH)
    contract = _json(ALERT_CONTRACT_PATH)
    frozen = set(governance["critical_failure_modes"])
    alerts = contract["critical_alerts"]
    assert set(alerts) == frozen

    alert_names = set()
    for failure_mode, definition in alerts.items():
        assert set(definition) == REQUIRED_ALERT_FIELDS, failure_mode
        assert definition["severity"] == "critical"
        assert definition["owner"].strip()
        assert definition["query"].strip()
        assert definition["threshold"].strip()
        assert re.fullmatch(r"\d+[smhd]", definition["sustain_window"])
        assert definition["deduplication_key"].startswith("p8/")
        assert definition["customer_impact"].strip()
        assert definition["runbook"].startswith("docs/runbooks/p8/")
        assert definition["runbook"].endswith(".md")
        assert len(definition["safe_first_actions"]) >= 2
        assert all(str(action).strip() for action in definition["safe_first_actions"])
        assert definition["escalation_condition"].strip()
        assert definition["alert"] not in alert_names
        alert_names.add(definition["alert"])

        lowered = definition["query"].lower()
        for forbidden in FORBIDDEN_LABEL_KEYS:
            assert f"{forbidden}=" not in lowered
            assert f'{forbidden}=~' not in lowered


def test_prometheus_alert_bundle_matches_contract_exactly() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    rules = _rules()
    assert isinstance(rules["groups"], list)
    assert {group["name"] for group in rules["groups"]} == {
        "doers.p8.slo.recording",
        "doers.p8.critical.alerts",
    }
    assert all(group["interval"] == "30s" for group in rules["groups"])

    bundle = _alert_rules()
    expected_names = {
        definition["alert"] for definition in contract["critical_alerts"].values()
    }
    assert set(bundle) == expected_names

    for failure_mode, definition in contract["critical_alerts"].items():
        rule = bundle[definition["alert"]]
        assert rule["expr"] == definition["query"]
        assert rule["for"] == definition["sustain_window"]
        assert rule["labels"]["severity"] == "critical"
        assert rule["labels"]["owner"] == definition["owner"]
        assert rule["labels"]["failure_mode"] == failure_mode
        assert rule["labels"]["dedupe_key"] == definition["deduplication_key"]
        assert rule["annotations"]["runbook"] == definition["runbook"]
        assert rule["annotations"]["customer_impact"]
        assert rule["annotations"]["safe_first_actions"]
        assert rule["annotations"]["escalation_condition"]


def test_alert_queries_use_only_frozen_metric_adapter_or_p8_recording_rules() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    allowed_metrics = set(contract["metric_adapter"].values())
    allowed_metrics |= {
        "doers_api_request_duration_milliseconds_bucket",
        "doers_api_request_duration_milliseconds_count",
    }
    recording = set(_recording_rules())

    token_re = re.compile(r"\bdoers(?:_[A-Za-z0-9_]+|:p8:[A-Za-z0-9_:]+)\b")
    expressions = [
        definition["query"] for definition in contract["critical_alerts"].values()
    ] + [rule["expr"] for rule in _recording_rules().values()]

    for expression in expressions:
        for token in token_re.findall(expression):
            assert token in allowed_metrics or token in recording, (token, expression)


def test_slo_contract_has_explicit_objectives_error_budgets_and_burn_policy() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    slos = contract["slos"]
    assert set(slos) == EXPECTED_SLOS

    for name, slo in slos.items():
        objective = float(slo["objective"])
        budget = float(slo["error_budget_fraction"])
        assert 0.0 < objective < 1.0, name
        assert abs((1.0 - objective) - budget) < 1e-12, name
        assert slo["window"] == "30d"
        assert slo["sli"].strip()

    assert slos["api_latency"]["threshold_ms"] == 1000
    assert slos["durable_queue_freshness"]["threshold_seconds"] == 300
    assert slos["lifecycle_completion"]["threshold_seconds"] == 900
    assert slos["backup_freshness"]["threshold_seconds"] == 86400

    policy = contract["error_budget_policy"]
    assert policy["fast_burn"] == {
        "burn_rate": 14.4,
        "short_window": "5m",
        "long_window": "1h",
        "severity": "critical",
    }
    assert policy["slow_burn"] == {
        "burn_rate": 6.0,
        "short_window": "30m",
        "long_window": "6h",
        "severity": "critical",
    }
    assert policy["remaining_budget_warning_fraction"] == 0.25
    assert policy["automatic_business_state_change"] is False


def test_required_slo_recording_rules_exist_and_use_frozen_budget_divisors() -> None:
    recording = _recording_rules()
    contract = _json(ALERT_CONTRACT_PATH)
    expected = set()
    for slo in contract["slos"].values():
        expected.update(slo.get("recording_rules", []))
    assert expected <= set(recording)

    assert recording["doers:p8:api_availability_burn_rate:5m"]["expr"].endswith("/ 0.001")
    assert recording["doers:p8:api_latency_burn_rate:5m"]["expr"].endswith("/ 0.01")
    assert recording["doers:p8:provider_error_burn_rate:5m"]["expr"].endswith("/ 0.005")
    assert 'le="1000"' in recording["doers:p8:api_latency_bad_ratio:5m"]["expr"]


def test_observability_pipeline_failure_is_detectable_without_business_authority() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    metric_contract = _json(METRIC_CONTRACT_PATH)
    observability = contract["critical_alerts"]["observability_pipeline_failure"]
    query = observability["query"]
    for profile in ("api", "worker", "maintenance", "beat"):
        assert f'profile="{profile}"' in query
    assert "absent_over_time" in query

    platform_names = {
        definition["name"] for definition in metric_contract["domains"]["platform"]
    }
    assert "doers.platform.observability.heartbeat" in platform_names
    assert '"doers.platform.observability.heartbeat"' in RUNTIME_METRICS_SOURCE
    assert "runtime_metrics().telemetry_heartbeat(profile=profile)" in METRICS_BOOTSTRAP_SOURCE
    assert contract["rule_backend"]["observability_is_business_authority"] is False
    assert contract["error_budget_policy"]["automatic_business_state_change"] is False


def test_p8a_preserves_hard_stops_and_does_not_claim_later_slice_markers() -> None:
    contract = _json(ALERT_CONTRACT_PATH)
    assert set(contract["hard_stops"]) == {
        "refund_provider_activation",
        "live_money_movement",
        "release",
        "deployment",
        "observability_rule_changes_business_authority",
        "alert_query_uses_high_cardinality_identity_labels",
    }

    doc = DOC_PATH.read_text(encoding="utf-8")
    for marker in (
        "P8_CRITICAL_ALERT_COVERAGE=PASS",
        "P8_SLO_CONTRACT=PASS",
        "P8_ALERT_ACTIONABILITY=PASS",
        "P8_OBSERVABILITY_PIPELINE_ALERT=PASS",
        "P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS",
        "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in doc
    assert "does **not** emit `P8_RUNBOOK_COVERAGE=PASS`" in doc
    assert "`P8_PRODUCTION_LIKE_OBSERVABILITY=PASS`" in doc
    assert "the final P8-F marker" in doc
