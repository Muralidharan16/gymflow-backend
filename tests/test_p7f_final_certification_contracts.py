from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/p7f-final-same-head-certification.yml"
DOC_PATH = ROOT / "docs/architecture/P7F_FINAL_CERTIFICATION.md"
BRANCH = "hardening/p7f-final-certification-temp"
P7_SLICE_HEAD = "916bc58e80f06e46311b3c3572e54268a06c82f8"
P7_SLICE_TREE = "b13592b0cd80a8112826c26af538e06d7369450f"

P7_JOBS = {
    "p7_governance": "./.github/workflows/p7-governance-api-runtime.yml",
    "p7_system": "./.github/workflows/p7s-system-control.yml",
    "p7_drain": "./.github/workflows/p7d-request-drain.yml",
    "p7_resource": "./.github/workflows/p7r-api-resource-shutdown.yml",
    "p7_worker": "./.github/workflows/p7w-worker-termination-inheritance.yml",
    "p7_orchestration": "./.github/workflows/p7o-production-like-orchestration.yml",
}

EXPECTED_NEEDS = {
    "contract",
    "architecture",
    "migration_hardening",
    "finance",
    "migration_lifecycle",
    "migration_preservation",
    "migration_adversarial",
    "migration_contracts",
    "migration_semantics",
    "p3",
    "p3a_general",
    "lifecycle_maintenance",
    "platform_maintenance",
    "p4b_opensearch",
    "p4b_evidence",
    "p4b_drift",
    "p4c_general",
    "p4c_notification",
    "p4d_refund",
    "p4e_contract",
    "p4e_operational",
    "p5_governance",
    "p5_worker_fencing",
    "p5_worker_crash_redelivery",
    "p5_provider_ack",
    "p5_dependency_loss",
    "p5_race_deadlock",
    "p5_compensation",
    "p6_governance",
    "p6_redis",
    "p6_broker",
    "p6_worker",
    "p6_poison",
    "p6_scheduler",
    *P7_JOBS.keys(),
}

MARKERS = (
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
)


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p7f_is_read_only_single_branch_certification() -> None:
    workflow = _workflow()
    assert workflow["on"]["push"]["branches"] == [BRANCH]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in ("kubectl apply", "helm upgrade", "gh release", "docker push"):
        assert forbidden not in source


def test_p7f_terminal_has_exact_40_gate_topology() -> None:
    workflow = _workflow()
    terminal = workflow["jobs"]["certify"]
    assert terminal["name"] == "Terminal immutable Final P7 decision"
    assert terminal["if"] == "${{ always() }}"
    needs = terminal["needs"]
    assert len(needs) == 40
    assert set(needs) == EXPECTED_NEEDS


def test_p7f_reuses_all_p7_gates_on_same_candidate() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    for job_name, reusable_path in P7_JOBS.items():
        job = jobs[job_name]
        assert job["uses"] == reusable_path
        if job_name == "p7_orchestration":
            assert job["with"]["certification_head"] == "${{ github.sha }}"
        else:
            assert "with" not in job


def test_p7f_serializes_p7_worker_reproof_after_direct_p6_worker() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["p7_worker"]["needs"] == "p6_worker"


def test_p7f_keeps_inherited_p6_same_head_runtime_gates() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    expected = {
        "p6_redis": "./.github/workflows/p6r-redis-production-contract.yml",
        "p6_broker": "./.github/workflows/p6b-broker-reconnect-pg16.yml",
        "p6_worker": "./.github/workflows/p6w-worker-shutdown-redelivery-pg16.yml",
        "p6_poison": "./.github/workflows/p6p-poison-message-pg16.yml",
        "p6_scheduler": "./.github/workflows/p6s-scheduler-ownership-pg16.yml",
    }
    for job_name, reusable_path in expected.items():
        job = jobs[job_name]
        assert job["uses"] == reusable_path
        assert job["with"]["certification_head"] == "${{ github.sha }}"


def test_p7f_contract_binds_fully_green_slice_head_and_tree() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    doc = DOC_PATH.read_text(encoding="utf-8")
    for value in (P7_SLICE_HEAD, P7_SLICE_TREE):
        assert value in source
        assert value in doc


def test_p7f_terminal_reconfirms_exact_immutable_candidate() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for required in (
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        'test "$(git rev-parse HEAD^{tree})" = "$(git write-tree)"',
        'test -z "$(git status --porcelain)"',
        "REQUIRED_RESULTS_JSON",
    ):
        assert required in source
    for marker in MARKERS:
        assert marker in source


def test_p7f_document_preserves_fail_closed_boundary() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "exactly 40 prerequisite results" in doc
    assert "real PostgreSQL 16" in doc
    assert "real Uvicorn process" in doc
    assert "does not authorize or perform a merge" in doc
    assert "deferred and fail-closed" in doc
