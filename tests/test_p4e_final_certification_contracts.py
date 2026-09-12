from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_JSON = ROOT / "docs/architecture/p4e_certification_matrix.json"
MATRIX_MD = ROOT / "docs/architecture/P4E_ACCEPTANCE_MATRIX.md"
INVENTORY = ROOT / "docs/architecture/p4_external_effect_inventory.json"
CONTRACT = ROOT / "docs/architecture/P4_EXTERNAL_EFFECTS_CONTRACT.md"
FINAL_WORKFLOW = (
    ROOT / ".github/workflows/p4e-final-external-effects-certification.yml"
)
PG16_WORKFLOW = ROOT / ".github/workflows/p4e-operational-snapshots-pg16.yml"

EXPECTED_GATES = {
    "inventory_truth": "certified",
    "attempt_is_not_success": "certified",
    "search_provider_and_reconciliation": "certified",
    "notification_delivery_webhook_and_reconciliation": "certified",
    "refund_obligation_authority_and_recovery": "certified",
    "refund_provider_execution": "deferred_fail_closed",
    "maintenance_only_aggregate_observability": "certified",
    "operator_recovery_safety": "certified",
    "p4e_migration_lifecycle": "certified",
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_p4e_machine_matrix_is_complete_and_all_evidence_paths_exist() -> None:
    matrix = _json(MATRIX_JSON)

    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P4E"
    assert matrix["governing_rule"] == "local_attempt_is_not_external_success"
    assert matrix["required_head_migration"] == "zf07d8e9f0a40"
    assert {entry["id"]: entry["status"] for entry in matrix["gates"]} == (
        EXPECTED_GATES
    )
    for gate in matrix["gates"]:
        assert gate["evidence"]
        for evidence in gate["evidence"]:
            assert (ROOT / evidence).is_file(), (gate["id"], evidence)

    follow_on = matrix["excluded_follow_on"]
    assert follow_on == {
        "phase": "Final P4",
        "scope": "same-head inherited P1/P2/P3 plus full P4 regression",
        "status": "not_claimed_by_p4e",
    }


def test_inventory_reports_certified_boundaries_without_hiding_refund_gap() -> None:
    inventory = _json(INVENTORY)
    events = {
        entry["event_type"]: entry for entry in inventory["lifecycle_external_events"]
    }

    assert inventory["schema_version"] == 3
    assert events["branch.search_index"]["certification_status"] == "certified"
    assert events["branch.search_deindex"]["certification_status"] == "certified"
    assert events["branch.member_notification"]["certification_status"] == "certified"
    assert events["branch.refund_required"]["certification_status"] == (
        "certified_boundary_provider_execution_deferred"
    )
    gaps = {entry["id"]: entry for entry in inventory["known_p4_gaps"]}
    assert set(gaps) == {"lifecycle_refund_provider_execution_deferred"}
    assert "provider refund execution" in gaps[
        "lifecycle_refund_provider_execution_deferred"
    ]["risk"]
    assert inventory["p4e_operational_observability"][
        "provider_refund_execution"
    ] == "deferred_fail_closed"


def test_p4e_observability_inventory_is_exact_and_identifier_free() -> None:
    observability = _json(INVENTORY)["p4e_operational_observability"]

    assert observability["database_capability"] == "lifecycle_maintenance_runtime"
    assert observability["snapshot_functions"] == [
        "app_secure.search_operational_snapshot()",
        "app_secure.refund_execution_operational_snapshot()",
    ]
    assert observability["metrics"] == [
        "doers.external_effect.snapshot.depth",
        "doers.external_effect.snapshot.oldest_age",
    ]
    assert observability["metric_attribute_keys"] == ["domain", "state"]
    assert set(observability["forbidden_metric_attributes"]) == {
        "tenant_id",
        "organization_id",
        "branch_id",
        "refund_id",
        "payment_id",
        "provider_reference_id",
        "correlation_id",
    }


def test_human_matrix_and_contract_preserve_p4e_hard_stops() -> None:
    matrix = MATRIX_MD.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    for phrase in (
        "one immutable commit",
        "provider refund execution explicitly deferred and fail-closed",
        "No live provider refund call or money movement",
        "numeric aggregates only",
        "belong only to the maintenance process and database capability",
        "separate final-P4 gate",
    ):
        assert phrase in matrix
    assert "P4B search and P4C notification delivery are certified" in contract
    assert "provider refund execution remains deferred and fail-closed" in contract


def test_both_p4e_workflows_target_the_same_branch_and_emit_exact_markers() -> None:
    matrix = _json(MATRIX_JSON)
    required = {
        entry["workflow"]: entry["marker"]
        for entry in matrix["required_same_head_checks"]
    }
    assert required == {
        ".github/workflows/p4e-operational-snapshots-pg16.yml": (
            "P4E_SLICE1B_PG16_CERTIFICATION=PASS"
        ),
        ".github/workflows/p4e-final-external-effects-certification.yml": (
            "P4E_FINAL_EXTERNAL_EFFECT_CERTIFICATION=PASS"
        ),
    }

    for path in (PG16_WORKFLOW, FINAL_WORKFLOW):
        workflow = path.read_text(encoding="utf-8")
        assert "hardening/p4e-operational-recovery-observability" in workflow
        assert "contents: read" in workflow
    for path, marker in required.items():
        assert marker in (ROOT / path).read_text(encoding="utf-8")
