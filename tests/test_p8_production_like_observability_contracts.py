from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE = ROOT / "docs/architecture/p8_observability_matrix.json"
METRICS = ROOT / "docs/architecture/p8_metric_contract.json"
ALERTS = ROOT / "docs/architecture/p8_alert_slo_contract.json"
RUNBOOKS = ROOT / "docs/architecture/p8_runbook_contract.json"
P8O = ROOT / "docs/architecture/p8_production_like_observability_contract.json"
DOC = ROOT / "docs/architecture/P8O_PRODUCTION_LIKE_OBSERVABILITY.md"
WORKFLOW = ROOT / ".github/workflows/p8o-production-like-observability.yml"

P8R_CERTIFIED_HEAD = "ecec7c21aa5f70331124796d81aab18b779e64b8"
P8R_CERTIFIED_TREE = "8de69d66087da6fda721479a029078d102cf4437"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_p8o_is_exactly_bound_to_certified_p8r_parent() -> None:
    contract = _json(P8O)
    assert contract["schema_version"] == 1
    assert contract["phase"] == "P8-O"
    assert contract["parent_certified_head"] == P8R_CERTIFIED_HEAD
    assert contract["parent_certified_tree"] == P8R_CERTIFIED_TREE
    proof = contract["proof_environment"]
    assert proof["postgresql_major"] == 16
    assert proof["redis_image"] == "redis:7-alpine"
    assert proof["otlp_transport"] == "http/protobuf"
    assert proof["celery_pool"] == "prefork"
    assert proof["disposable_only"] is True
    assert proof["localhost_only_destructive_faults"] is True
    assert proof["mock_only_proof_forbidden"] is True


def test_all_frozen_critical_modes_have_production_like_metric_alert_runbook_binding() -> None:
    governance = _json(GOVERNANCE)
    metric_contract = _json(METRICS)
    alert_contract = _json(ALERTS)["critical_alerts"]
    runbook_contract = _json(RUNBOOKS)["runbooks"]
    p8o = _json(P8O)

    frozen = set(governance["critical_failure_modes"])
    proof = p8o["critical_failure_mode_proof"]
    assert set(proof) == frozen == set(alert_contract) == set(runbook_contract)

    metric_names = {
        definition["name"]
        for definitions in metric_contract["domains"].values()
        for definition in definitions
    }
    scenarios = p8o["real_runtime_scenarios"]

    for failure_mode, binding in proof.items():
        assert binding["scenario"] in scenarios, failure_mode
        assert binding["metric"] in metric_names, failure_mode
        assert binding["alert"] == alert_contract[failure_mode]["alert"]
        assert binding["runbook"] == alert_contract[failure_mode]["runbook"]
        assert binding["runbook"] == runbook_contract[failure_mode]
        assert (ROOT / binding["runbook"]).is_file()


def test_every_p8o_scenario_is_real_or_explicit_external_authority_ingress() -> None:
    scenarios = _json(P8O)["real_runtime_scenarios"]
    assert len(scenarios) >= 13
    permitted_kinds = {
        "real_postgresql_persisted_fault_state",
        "real_postgresql_stuck_transition",
        "real_redis_delivery_backlog",
        "real_postgresql_pool_exhaustion",
        "real_postgresql_service_stop_start",
        "real_redis_container_stop_start",
        "real_celery_prefork_process_death",
        "real_provider_effect_plus_database_ack_disconnect",
        "real_redis_ownership_contention",
        "infrastructure_monitor_signal_ingress",
        "real_asgi_middleware_request",
        "real_loopback_http_timeout",
        "real_otlp_connection_failure",
    }
    for name, scenario in scenarios.items():
        assert scenario["kind"] in permitted_kinds, name
        assert scenario["evidence"], name
        assert scenario["proves"], name
        for path in scenario["evidence"]:
            assert (ROOT / path).is_file(), (name, path)


def test_p8o_workflow_runs_real_fault_harnesses_and_real_otlp_receiver() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    required = (
        "redis:7-alpine",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/p8o_otlp_collector.py",
        "tests/test_p4e_operational_snapshots_runtime.py",
        "tests/test_p5d_dependency_loss_runtime.py",
        "tests/test_p5w2_worker_crash_redelivery_runtime.py",
        "tests/test_p8_production_like_observability_runtime.py",
        "scripts/ci/p8o_verify_worker_capture.py",
        "P8O_PROCESS_FAULTS: '1'",
        "P5D_PROCESS_FAULTS: '1'",
        "P5W2_PROCESS_FAULTS: '1'",
        "python -s -m alembic -c alembic.ini upgrade head",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "git diff --exit-code",
        "git diff --cached --exit-code",
    )
    for value in required:
        assert value in source, value
    assert "mock" not in source.lower() or "mock-only" in DOC.read_text(encoding="utf-8").lower()


def test_p8o_workflow_has_three_independent_real_runtime_jobs_before_decision() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for job in ("operational-signals:", "dependency-faults:", "worker-redelivery:"):
        assert job in source
    assert "needs: [operational-signals, dependency-faults, worker-redelivery]" in source
    assert "timeout-minutes:" in source
    assert "P8R_CERTIFIED_HEAD" in source
    assert "P8R_CERTIFIED_TREE" in source


def test_p8o_authority_and_hard_stops_are_explicit() -> None:
    contract = _json(P8O)
    assert contract["authority_assertions"] == {
        "postgresql_remains_durable_business_authority": True,
        "redis_is_delivery_coordination_only": True,
        "observability_is_evidence_only": True,
        "provider_ambiguity_never_authorizes_blind_retry": True,
        "telemetry_outage_never_rolls_back_or_blocks_durable_commit": True,
        "refund_provider_execution": "deferred_fail_closed",
    }
    assert set(contract["hard_stops"]) == {
        "non_disposable_destructive_fault_target",
        "mock_only_failure_injection",
        "observability_signal_used_to_mutate_business_state",
        "blind_provider_retry_after_ambiguous_outcome",
        "refund_provider_activation",
        "live_money_movement",
        "release",
        "deployment",
    }


def test_p8o_runtime_source_has_disposable_guards_and_actual_fault_actions() -> None:
    runtime = (ROOT / "tests/test_p8_production_like_observability_runtime.py").read_text(
        encoding="utf-8"
    )
    collector = (ROOT / "scripts/ci/p8o_otlp_collector.py").read_text(encoding="utf-8")
    for token in (
        'P8O_PROCESS_FAULTS") != "1"',
        '"gymflow_p8o_test"',
        '"docker", "stop"',
        '"docker", "start"',
        '"systemctl", "stop", "postgresql"',
        '"systemctl", "start", "postgresql"',
        "create_async_engine(",
        "pool_size=1",
        "pool_timeout=0.2",
        "BeatOwnershipLease()",
        "urlopen(endpoint, timeout=0.05)",
        "RequestObservabilityMiddleware",
        "force_flush_runtime_metrics",
    ):
        assert token in runtime, token
    assert "ExportMetricsServiceRequest" in collector
    assert 'self.path != "/v1/metrics"' in collector


def test_p8o_document_emits_only_slice_markers_not_final_phase_gate() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for marker in (
        "P8_PRODUCTION_LIKE_OBSERVABILITY=PASS",
        "P8_REAL_DEPENDENCY_FAILURE_INJECTION=PASS",
        "P8_REAL_WORKER_REDELIVERY_OBSERVABILITY=PASS",
        "P8_REAL_OTLP_EXPORT=PASS",
        "P8_AUTHORITY_SURVIVES_OBSERVABILITY_FAILURE=PASS",
        "P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS",
        "P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in doc
    assert "does **not** emit `P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS`" in doc
    assert "does not emit the final `P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS`" in doc
