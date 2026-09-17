import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p10_performance_security_final_certification_matrix.json"
SCOPE_PATH = ROOT / "docs/architecture/P10_PERFORMANCE_SECURITY_FINAL_PRODUCTION_SCOPE_AND_GATES.md"
ACCEPTANCE_PATH = ROOT / "docs/architecture/P10_ACCEPTANCE_MATRIX.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p10-governance-performance-security.yml"

BASE_COMMIT = "33abd2bad81c65ac998b91726cc314ab54080010"
BASE_TREE = "a6a0a87ebbaa251482c67f1bc4cbd9dc8ab7d1e0"
BRANCH = "hardening/p10-performance-security-final-production-certification"
ALEMBIC_HEAD = "zk07d8e9f0a45"


def _matrix():
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_p10_baseline_and_slice_order_are_frozen():
    matrix = _matrix()
    assert matrix["phase"] == "P10"
    assert matrix["base_commit"] == BASE_COMMIT
    assert matrix["base_tree"] == BASE_TREE
    assert matrix["branch"] == BRANCH
    assert matrix["alembic_head"] == ALEMBIC_HEAD
    assert matrix["slices"] == [
        "P10-G", "P10-B", "P10-L", "P10-Q", "P10-D",
        "P10-S", "P10-X", "P10-H", "P10-F",
    ]
    assert matrix["hard_gate"] == (
        "representative_performance_security_container_and_soak_certified_on_one_immutable_head"
    )


def test_budget_freeze_prevents_moving_goalposts():
    budget = _matrix()["budget_freeze"]
    assert budget["numeric_budgets_must_be_frozen_before_load_queue_or_soak_certification"] is True
    assert budget["budget_loosening_to_make_candidate_pass_forbidden"] is True
    assert budget["baseline_and_budget_artifact_sha_bound"] is True
    assert budget["real_postgresql_16_required"] is True
    assert budget["real_redis_required"] is True
    assert budget["production_container_required"] is True


def test_performance_queue_query_and_soak_contracts_are_mandatory():
    matrix = _matrix()
    load = matrix["representative_load"]
    concurrency = matrix["concurrency_stress"]
    queue = matrix["queue_backlog_recovery"]
    database = matrix["database_and_api_efficiency"]
    soak = matrix["soak"]

    assert load["p50_p95_p99_latency_required"] is True
    assert load["throughput_required"] is True
    assert load["five_xx_without_fault_injection_forbidden"] is True
    assert concurrency["no_lost_updates_required"] is True
    assert concurrency["no_duplicate_financial_effects_required"] is True
    assert queue["worker_replacement_during_backlog_required"] is True
    assert queue["no_lost_durable_work_required"] is True
    assert database["explain_analyze_buffers_for_critical_queries_required"] is True
    assert database["n_plus_one_detection_required"] is True
    assert database["bounded_pagination_required"] is True
    assert soak["long_running_test_required"] is True
    assert soak["no_restart_as_success_strategy"] is True


def test_security_and_container_hardening_fail_closed():
    matrix = _matrix()
    scans = matrix["security_scans"]
    config = matrix["secrets_and_config"]
    container = matrix["production_container"]

    assert scans["sbom_required"] is True
    assert scans["critical_or_high_unresolved_runtime_vulnerability_is_hard_stop"] is True
    assert scans["verified_secret_is_hard_stop"] is True
    assert config["mandatory_secret_absence_must_fail_closed"] is True
    assert config["production_default_credentials_forbidden"] is True
    assert config["provider_execution_remains_fail_closed"] is True
    assert container["non_root_runtime_required"] is True
    assert container["healthcheck_required"] is True
    assert container["reload_forbidden"] is True
    assert container["graceful_sigterm_required"] is True
    assert container["read_only_root_filesystem_runtime_test_required"] is True
    assert container["no_new_privileges_required"] is True
    assert container["drop_all_linux_capabilities_required"] is True
    assert container["cpu_memory_and_pid_resource_controls_required"] is True


def test_final_certification_is_one_immutable_head():
    final = _matrix()["final_certification"]
    assert final["one_commit_acceptance_matrix_required"] is True
    assert final["all_p1_p9_inherited_gates_required"] is True
    assert final["all_p10_slice_gates_required"] is True
    assert final["same_head_sha_required"] is True
    assert final["same_tree_required"] is True
    assert final["no_post_certification_mutation_allowed"] is True


def test_authority_and_hard_stops_remain_strict():
    matrix = _matrix()
    authority = matrix["authority"]
    hard_stops = set(matrix["hard_stops"])

    assert authority["durable_business_work"] == "postgresql"
    assert authority["redis_celery_beat"] == "delivery_coordination_only"
    assert authority["refund_provider_execution"] == "deferred_fail_closed"
    assert authority["production_data_copy_to_ci"] == "forbidden"
    assert authority["live_load_test_against_production"] == "forbidden"

    required = {
        "performance_budget_loosened_after_freeze_to_make_candidate_pass",
        "lost_update_duplicate_financial_effect_or_stuck_lifecycle_under_stress",
        "unresolved_critical_or_high_runtime_dependency_or_container_vulnerability",
        "verified_secret_or_live_credential_present_in_repository_or_ci_evidence",
        "production_container_runs_as_root_or_privileged",
        "soak_exhibits_unbounded_memory_connection_latency_or_backlog_growth",
        "refund_provider_activation",
        "release",
        "production_deployment",
    }
    assert required <= hard_stops


def test_scope_and_acceptance_documents_bind_terminal_markers():
    scope = SCOPE_PATH.read_text(encoding="utf-8")
    acceptance = ACCEPTANCE_PATH.read_text(encoding="utf-8")

    assert BASE_COMMIT in scope
    assert BASE_TREE in scope
    assert "P10-B" in scope
    assert "budget" in scope.lower()
    assert "non-root" in scope
    assert "read-only-root-filesystem" in scope

    for marker in (
        "P10_GOVERNANCE=PASS",
        "P10_BASELINE_BUDGETS=PASS",
        "P10_REPRESENTATIVE_LOAD=PASS",
        "P10_CONCURRENCY_STRESS=PASS",
        "P10_QUEUE_BACKLOG_RECOVERY=PASS",
        "P10_QUERY_PERFORMANCE=PASS",
        "P10_SOAK=PASS",
        "P10_SECURITY_SCANS=PASS",
        "P10_SECRETS_CONFIG_FAIL_CLOSED=PASS",
        "P10_CONTAINER_HARDENING=PASS",
        "P10_P1_P9_INHERITED=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
        "P10_FINAL_PRODUCTION_CERTIFICATION=PASS",
    ):
        assert marker in acceptance


def test_governance_workflow_is_exact_head_and_inherits_p9():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert BRANCH in text
    assert BASE_COMMIT in text
    assert BASE_TREE in text
    assert "P10_CANDIDATE_SHA" in text
    assert "tests/test_p9_governance_contracts.py" in text
    assert "tests/test_p9f_final_certification_contracts.py" in text
    assert "scripts/verify_head_workflow_bootstrap.py" in text
    assert "P10_GOVERNANCE=PASS" in text
    assert "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in text
