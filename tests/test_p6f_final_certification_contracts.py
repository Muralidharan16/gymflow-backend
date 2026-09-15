from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
P5F_WORKFLOW = ROOT / ".github/workflows/p5f-final-same-head-certification.yml"
P6F_WORKFLOW = ROOT / ".github/workflows/p6f-final-same-head-certification.yml"
CONTRACT = ROOT / "docs/architecture/P6F_FINAL_CERTIFICATION.md"
P6S_PREDECESSOR = "4170b3a3d3438294f3b7999316f55d20749894c3"
P6S_TREE = "3eff2d54ce6ea1b58cea94e8e2574430a93a9fe7"
P6F_BRANCH = "hardening/p6f-final-certification-temp"

P6 = {
    "p6_governance": (
        ".github/workflows/p6-governance-production-readiness.yml",
        "governance",
        False,
    ),
    "p6_redis": (
        ".github/workflows/p6r-redis-production-contract.yml",
        "redis-production-contract",
        True,
    ),
    "p6_broker": (
        ".github/workflows/p6b-broker-reconnect-pg16.yml",
        "broker-reconnect",
        True,
    ),
    "p6_worker": (
        ".github/workflows/p6w-worker-shutdown-redelivery-pg16.yml",
        "worker-shutdown-redelivery",
        True,
    ),
    "p6_poison": (
        ".github/workflows/p6p-poison-message-pg16.yml",
        "poison-message",
        True,
    ),
    "p6_scheduler": (
        ".github/workflows/p6s-scheduler-ownership-pg16.yml",
        "scheduler-ownership",
        True,
    ),
}

REQUIRED_MARKERS = (
    "P6F_INHERITED_P1_P5_CRITICAL_GATES=PASS",
    "P6_REDIS_PRODUCTION_CONTRACT=PASS",
    "P6_BROKER_RECONNECT_RECOVERY=PASS",
    "P6_WORKER_SIGTERM_SAFE=PASS",
    "P6_LATE_ACK_REDELIVERY_SAFE=PASS",
    "P6_POISON_MESSAGE_CONTAINED=PASS",
    "P6_BEAT_SINGLE_OWNER=PASS",
    "P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS",
    "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
    "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    "P6F_FINAL_SAME_HEAD_CERTIFICATION=PASS",
)


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _p5_gate_jobs() -> dict[str, dict]:
    jobs = _workflow(P5F_WORKFLOW)["jobs"]
    return {
        name: details
        for name, details in jobs.items()
        if name not in {"contract", "certify"}
    }


def test_p6f_reuses_complete_p5f_27_gate_topology_without_drift() -> None:
    inherited = _p5_gate_jobs()
    p6f_jobs = _workflow(P6F_WORKFLOW)["jobs"]

    assert len(inherited) == 27
    for job_id, p5_job in inherited.items():
        p6_job = p6f_jobs[job_id]
        assert p6_job["uses"] == p5_job["uses"], job_id
        assert p6_job.get("with") == p5_job.get("with"), job_id


def test_p6f_adds_all_six_p6_reusable_gates_and_exact_sha_binding() -> None:
    workflow = _workflow(P6F_WORKFLOW)
    jobs = workflow["jobs"]

    assert workflow["on"]["push"]["branches"] == [P6F_BRANCH]
    assert workflow["permissions"] == {"contents": "read"}

    expected_jobs = {"contract", "certify", *_p5_gate_jobs(), *P6}
    assert set(jobs) == expected_jobs

    for job_id, (workflow_path, expected_job, head_aware) in P6.items():
        job = jobs[job_id]
        assert job["uses"] == f"./{workflow_path}"
        called = _workflow(ROOT / workflow_path)
        assert "workflow_call" in called["on"]
        assert set(called["jobs"]) == {expected_job}
        assert called["permissions"] == {"contents": "read"}
        if head_aware:
            assert job["with"] == {"certification_head": "${{ github.sha }}"}
            input_contract = called["on"]["workflow_call"]["inputs"]["certification_head"]
            assert input_contract["type"] == "string"
        else:
            assert "with" not in job


def test_terminal_fanin_requires_contract_plus_all_33_reusable_gates() -> None:
    jobs = _workflow(P6F_WORKFLOW)["jobs"]
    terminal = jobs["certify"]
    expected_prerequisites = {"contract", *_p5_gate_jobs(), *P6}

    assert len(_p5_gate_jobs()) + len(P6) == 33
    assert terminal["if"] == "${{ always() }}"
    assert set(terminal["needs"]) == expected_prerequisites
    assert "continue-on-error" not in terminal

    source = P6F_WORKFLOW.read_text(encoding="utf-8")
    assert 'details["result"] != "success"' in source
    assert 'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"' in source
    assert 'test "$(git rev-parse HEAD^{tree})" = "$(git write-tree)"' in source
    assert 'test -z "$(git status --porcelain)"' in source
    for marker in REQUIRED_MARKERS:
        assert marker in source


def test_contract_job_binds_certified_p6s_predecessor_and_current_head() -> None:
    source = P6F_WORKFLOW.read_text(encoding="utf-8")
    assert f"git cat-file -e '{P6S_PREDECESSOR}^{{commit}}'" in source
    assert P6S_TREE in source
    assert f"git merge-base --is-ancestor {P6S_PREDECESSOR} HEAD" in source
    assert "tests/test_p5f_final_certification_contracts.py" in source
    assert "tests/test_p6f_final_certification_contracts.py" in source
    assert "scripts/verify_alembic_graph.py" in source
    assert "scripts/verify_head_workflow_bootstrap.py" in source


def test_contract_preserves_authority_boundaries_and_requires_separate_merge_decision() -> None:
    contract = CONTRACT.read_text(encoding="utf-8")

    for phrase in (
        "PostgreSQL remains the durable business authority",
        "Redis, Celery and Beat remain delivery and coordination mechanisms only",
        "no live refund provider execution",
        "no live money movement",
        "separate explicit exact-candidate merge authorization",
        "Any candidate change invalidates the final same-head proof",
        "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
    ):
        assert phrase in contract

    for forbidden in ("merge or retarget", "tag or release", "deployment"):
        assert forbidden in contract


def test_frozen_p6_governance_matrix_is_not_rewritten_as_certification_output() -> None:
    matrix = (ROOT / "docs/architecture/p6_production_readiness_matrix.json").read_text(
        encoding="utf-8"
    )
    assert '"p6_certification": "not_yet_certified"' in matrix
    contract = CONTRACT.read_text(encoding="utf-8")
    assert "does not add business behavior" in contract
