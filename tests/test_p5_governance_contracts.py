from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "docs" / "architecture" / "P5_CONCURRENCY_CRASH_SCOPE_AND_GATES.md"
ACCEPTANCE = ROOT / "docs" / "architecture" / "P5_ACCEPTANCE_MATRIX.md"
MATRIX = ROOT / "docs" / "architecture" / "p5_fault_injection_matrix.json"
WORKFLOW = ROOT / ".github" / "workflows" / "p5-governance-fault-matrix.yml"
P4_CONTRACT = ROOT / "docs" / "architecture" / "P4_EXTERNAL_EFFECTS_CONTRACT.md"
P4E_ACCEPTANCE = ROOT / "docs" / "architecture" / "P4E_ACCEPTANCE_MATRIX.md"
P4F_CERTIFICATION = ROOT / "docs" / "architecture" / "P4F_FINAL_CERTIFICATION.md"

P4_MERGE_BASE = "99de1600636979fa355f8ba8ff665f98dc8148bf"
P4_TREE = "766243d0bdd0ccd1377d57ae726222f04ff4cf1c"

EXPECTED_FAULTS = {
    "worker_death_before_db_commit",
    "worker_death_after_db_commit_before_task_ack",
    "provider_success_db_ack_failure",
    "duplicate_celery_or_outbox_delivery",
    "lease_expiry_and_reclaim",
    "redis_or_network_loss",
    "database_disconnect",
    "deadlock_and_concurrent_lifecycle_transition",
    "finance_lifecycle_race",
    "compensation_crash_and_replay",
}

EXPECTED_HARD_GATE = [
    "no_lost_updates",
    "no_duplicate_financial_effects",
    "no_stuck_transitions",
    "no_unrecoverable_jobs",
]


def _load_matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_p5_identity_and_exact_p4_merge_base_are_frozen() -> None:
    scope = SCOPE.read_text(encoding="utf-8").lower()
    acceptance = ACCEPTANCE.read_text(encoding="utf-8").lower()
    matrix = _load_matrix()

    assert "p5 concurrency, crash and durable-worker fault tolerance" in scope
    assert "not the real-provider-adapter phase" in scope
    assert "not one of the historical\nfinance core phases" in scope
    assert matrix["phase"] == "P5 concurrency, crash and durable-worker fault tolerance"
    assert matrix["governance_stage"] == "P5-G"
    assert matrix["p4_merge_base_sha"] == P4_MERGE_BASE
    assert matrix["p4_certified_tree"] == P4_TREE
    assert P4_MERGE_BASE in scope and P4_MERGE_BASE in acceptance
    assert P4_TREE in scope and P4_TREE in acceptance


def test_p5_fault_inventory_and_hard_gate_are_exact() -> None:
    matrix = _load_matrix()
    scenarios = matrix["fault_scenarios"]

    assert len(scenarios) == len(EXPECTED_FAULTS)
    assert {scenario["id"] for scenario in scenarios} == EXPECTED_FAULTS
    assert matrix["hard_gate"] == EXPECTED_HARD_GATE

    for scenario in scenarios:
        assert scenario["slice"] in {"P5-W", "P5-E", "P5-D", "P5-R", "P5-C"}
        assert scenario["injection_point"].strip()
        assert scenario["expected_durable_state"].strip()
        assert len(scenario["required_proof"]) >= 3
        assert scenario["p5_certification"] == "not_yet_certified"


def test_p5_scope_requires_decisive_restart_concurrency_and_durable_evidence() -> None:
    scope = SCOPE.read_text(encoding="utf-8").lower()
    acceptance = ACCEPTANCE.read_text(encoding="utf-8").lower()

    for phrase in (
        "postgresql 16",
        "independent processes or connections",
        "separate verification identity",
        "stale-owner rejection",
        "bounded recovery route",
        "one immutable git sha",
        "cannot be the only evidence",
    ):
        assert phrase in scope

    for phrase in (
        "worker death before db commit",
        "worker death after db commit",
        "provider success / db acknowledgement failure",
        "duplicate celery/outbox delivery",
        "lease expiry and reclaim",
        "redis/broker/network loss",
        "db disconnects",
        "deadlocks/concurrent lifecycle transitions",
        "finance/lifecycle races",
        "compensation crash/replay",
        "same-head proof",
    ):
        assert phrase in acceptance


def test_p5_hard_stops_preserve_security_and_deferred_refund_provider() -> None:
    scope = SCOPE.read_text(encoding="utf-8").lower()
    matrix = _load_matrix()

    for phrase in (
        "live-money activation",
        "refund-provider call",
        "adding or selecting a new provider adapter",
        "arbitrary operator mutation to terminal success",
        "rls weakening",
        "bypassrls",
        "broad worker grants",
        "redis locks or celery acknowledgement as business authority",
        "unbounded retries",
        "tag, release, merge or deployment",
    ):
        assert phrase in scope

    inherited = matrix["inherited_boundaries"]
    assert inherited["provider_refund_execution"] == "deferred_fail_closed"
    assert inherited["live_money_activation"] == "forbidden"
    assert inherited["production_deployment"] == "forbidden"
    assert inherited["queue_or_redis_as_business_authority"] == "forbidden"
    assert inherited["runtime_privilege_widening"] == "forbidden"


def test_p5_durable_surface_inventory_points_to_real_repository_surfaces() -> None:
    surfaces = _load_matrix()["durable_surfaces"]
    assert {surface["id"] for surface in surfaces} == {
        "lifecycle_outbox",
        "transactional_outbox",
        "notification_commands",
        "refund_execution_commands",
        "lifecycle_compensation",
    }
    assert all(surface["p5_certification"] == "not_yet_certified" for surface in surfaces)

    expected_sources = (
        ROOT / "app" / "tasks" / "branch_outbox_poller.py",
        ROOT / "app" / "tasks" / "outbox_poller.py",
        ROOT / "app" / "services" / "notification_delivery_service.py",
        ROOT / "app" / "services" / "branch_lifecycle_service.py",
        ROOT / "app" / "finance_core" / "models" / "foundation.py",
    )
    assert all(path.is_file() for path in expected_sources)


def test_p5_keeps_p4_provider_truth_and_refund_deferral_explicit() -> None:
    p4_contract = P4_CONTRACT.read_text(encoding="utf-8").lower()
    p4e = P4E_ACCEPTANCE.read_text(encoding="utf-8").lower()
    p4f = P4F_CERTIFICATION.read_text(encoding="utf-8").lower()

    assert "a local command" in p4_contract
    assert "is not proof that the external business effect succeeded" in p4_contract
    assert "provider refund execution explicitly deferred and fail-closed" in p4e
    assert "refund-provider execution remains explicitly `deferred_fail_closed`" in p4f


def test_p5_governance_workflow_is_read_only_and_reproves_inherited_contracts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "branches:\n      - main" in workflow
    assert "contents: read" in workflow
    assert "p5_fault_injection_matrix.json" in workflow
    assert "tests/test_p5_governance_contracts.py" in workflow
    assert "tests/test_p4f_final_certification_contracts.py" in workflow
    assert "scripts/verify_alembic_graph.py" in workflow
    assert "scripts/verify_head_workflow_bootstrap.py" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "git diff --exit-code" in workflow
    assert "P5_GOVERNANCE_FAULT_MATRIX=PASS" in workflow
