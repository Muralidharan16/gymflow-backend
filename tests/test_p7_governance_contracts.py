from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p7_api_runtime_matrix.json"
SCOPE_PATH = ROOT / "docs/architecture/P7_API_RUNTIME_SCOPE_AND_GATES.md"
ACCEPTANCE_PATH = ROOT / "docs/architecture/P7_ACCEPTANCE_MATRIX.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p7-governance-api-runtime.yml"

P6_MERGE_BASE = "561510eed6244cee268215ff1c53af72a636a601"
P6_CERTIFIED_TREE = "6ae7a92bd06100434b80aed98772147f2bbeaef4"
P7_BRANCH = "hardening/p7-api-runtime-graceful-deployment"

FINAL_MARKERS = {
    "P7_PRESTOP_AUTHORIZATION=PASS",
    "P7_LIVENESS_READINESS_SEPARATED=PASS",
    "P7_READINESS_FALSE_BEFORE_SHUTDOWN=PASS",
    "P7_NEW_REQUEST_DRAIN=PASS",
    "P7_INFLIGHT_COMPLETION=PASS",
    "P7_API_RESOURCE_SHUTDOWN=PASS",
    "P7_WORKER_TERMINATION_RECOVERABLE=PASS",
    "P7_PRODUCTION_LIKE_ROLLOUT=PASS",
    "P7_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
    "P7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    "P7F_FINAL_SAME_HEAD_CERTIFICATION=PASS",
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p7_binds_exact_merged_p6_baseline() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P7"
    assert matrix["base_commit"] == P6_MERGE_BASE
    assert matrix["base_tree"] == P6_CERTIFIED_TREE
    assert matrix["slices"] == ["P7-G", "P7-S", "P7-D", "P7-R", "P7-W", "P7-O", "P7-F"]


def test_p7_freezes_secure_prestop_and_probe_split() -> None:
    system = _matrix()["system_endpoints"]
    prestop = system["prestop"]
    assert prestop["path"] == "/_system/preStop"
    assert prestop["dedicated_secret_required_in_production"] is True
    assert prestop["tenant_jwt_is_not_authorization"] is True
    assert prestop["missing_or_invalid_secret_must_not_drain"] is True
    assert system["liveness"] == {
        "dependency_sensitive": False,
        "must_remain_successful_while_draining": True,
    }
    assert system["readiness"] == {
        "must_fail_immediately_when_draining": True,
        "http_failure_status": 503,
    }


def test_p7_freezes_drain_and_resource_shutdown_contracts() -> None:
    matrix = _matrix()
    assert matrix["drain"] == {
        "reject_new_business_requests": True,
        "complete_admitted_inflight_requests": True,
        "exclude_system_requests_from_inflight": True,
        "repeated_prestop_idempotent": True,
        "bounded_shutdown_deadline": True,
    }
    assert matrix["resource_shutdown"] == {
        "supervisor": True,
        "redis": True,
        "api_async_database_engine": True,
        "api_sync_database_engine_if_present": True,
        "idempotent": True,
    }


def test_p7_inherits_p6_authority_and_worker_recoverability() -> None:
    matrix = _matrix()
    assert matrix["authority"] == {
        "durable_business_work": "postgresql",
        "redis_celery_beat": "delivery_coordination_only",
        "refund_provider_execution": "deferred_fail_closed",
    }
    assert matrix["worker_termination"] == {
        "inherit_p6_warm_sigterm": True,
        "inherit_p6_late_ack_redelivery": True,
        "durable_work_must_remain_recoverable": True,
    }


def test_p7_decisive_evidence_requires_real_process_orchestration() -> None:
    evidence = _matrix()["decisive_evidence"]
    assert evidence["real_api_process_required"] is True
    assert evidence["mock_only_rollout_proof_forbidden"] is True
    assert evidence["separate_liveness_readiness_required"] is True
    assert evidence["slow_inflight_request_required"] is True
    assert evidence["authorized_prestop_required"] is True
    assert evidence["new_request_rejection_required"] is True
    assert evidence["graceful_sigterm_exit_required"] is True
    assert evidence["resource_cleanup_required"] is True


def test_p7_acceptance_locks_terminal_markers_and_hard_stops() -> None:
    acceptance = ACCEPTANCE_PATH.read_text(encoding="utf-8")
    scope = SCOPE_PATH.read_text(encoding="utf-8")
    for marker in FINAL_MARKERS:
        assert marker in acceptance
    for phrase in (
        "A public or ordinary authenticated application caller",
        "liveness=200 and readiness=503",
        "newly arriving ordinary business requests are rejected with 503",
        "system liveness/readiness/preStop requests are not counted",
        "API SQLAlchemy async pool is disposed",
        "Mocks may validate authorization/parsing but may not be the decisive rollout/shutdown gate",
    ):
        assert phrase in scope
    assert set(_matrix()["hard_stops"]) == {
        "public_prestop",
        "combined_liveness_readiness_semantics",
        "new_business_work_after_draining",
        "p6_worker_semantic_weakening",
        "database_credential_broadening",
        "release",
        "deployment",
        "refund_provider_activation",
        "live_money_movement",
    }


def test_p7_governance_workflow_is_exact_base_and_branch_bound() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P7_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"governance"}
    job = workflow["jobs"]["governance"]
    assert job["env"]["P6_MERGE_BASE"] == P6_MERGE_BASE
    assert job["env"]["P6_CERTIFIED_TREE"] == P6_CERTIFIED_TREE
    assert "git merge-base --is-ancestor \"${P6_MERGE_BASE}\" HEAD" in source
    assert "tests/test_p7_governance_contracts.py" in source
    assert "tests/test_p6f_final_certification_contracts.py" in source
    assert "P7_GOVERNANCE_API_RUNTIME=PASS" in source
    assert "P7_PRODUCTION_LIKE_ROLLOUT=GOVERNANCE_FROZEN" in source
    assert "P7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source
