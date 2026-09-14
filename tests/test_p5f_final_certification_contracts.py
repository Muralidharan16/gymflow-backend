from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p5f_certification_matrix.json"
CONTRACT_PATH = ROOT / "docs/architecture/P5F_FINAL_CERTIFICATION.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p5f-final-same-head-certification.yml"
HEAD = "zj07d8e9f0a44"

INHERITED = {
    "architecture": (".github/workflows/architecture-hardening-contracts.yml", {"architecture-contracts"}),
    "migration_hardening": (
        ".github/workflows/hardening-ci.yml",
        {"static-hardening-contracts", "fresh-lineage-and-regressions", "platform-billing-regressions"},
    ),
    "finance": (".github/workflows/finance-hardening-ci.yml", {"finance-core-regressions"}),
    "migration_lifecycle": (".github/workflows/migration-lifecycle-ci.yml", {"full-lifecycle"}),
    "migration_preservation": (".github/workflows/migration-data-preservation-ci.yml", {"legacy-address-to-branch"}),
    "migration_adversarial": (".github/workflows/migration-adversarial-safety-ci.yml", {"adversarial-00f"}),
    "migration_contracts": (".github/workflows/migration-lifecycle-contracts.yml", {"lifecycle-contracts"}),
    "migration_semantics": (".github/workflows/migration-semantics-inventory.yml", {"inventory"}),
    "p3": (
        ".github/workflows/p3e-certification.yml",
        {"tenant-context-pool-isolation", "inherited-p3abc", "inherited-p3d", "migration-lifecycle", "general-regressions"},
    ),
    "p3a_general": (".github/workflows/p3a-general-regressions.yml", {"general-regressions"}),
    "lifecycle_maintenance": (".github/workflows/lifecycle-maintenance-production-boundary.yml", {"maintenance-boundary"}),
    "platform_maintenance": (".github/workflows/platform-maintenance-production-boundary.yml", {"platform-maintenance-boundary"}),
    "p4b_opensearch": (".github/workflows/p4b-opensearch-live.yml", {"live-opensearch"}),
    "p4b_evidence": (".github/workflows/p4b-search-evidence-pg16.yml", {"search-evidence"}),
    "p4b_drift": (".github/workflows/p4b-search-drift-pg16.yml", {"drift-repair"}),
    "p4c_general": (".github/workflows/p4c-general-regression.yml", {"general-regressions"}),
    "p4c_notification": (".github/workflows/p4c-notification-storage-pg16.yml", {"notification-storage"}),
    "p4d_refund": (".github/workflows/p4d-refund-authority-pg16.yml", {"refund-authority"}),
    "p4e_contract": (".github/workflows/p5f-inherited-p4e-contract.yml", {"p4e-contract-current-head"}),
    "p4e_operational": (".github/workflows/p5f-inherited-p4e-operational-pg16.yml", {"p4e-operational-current-head"}),
}

P5 = {
    "p5_governance": (".github/workflows/p5-governance-fault-matrix.yml", {"governance"}),
    "p5_worker_fencing": (".github/workflows/p5w-worker-fencing-pg16.yml", {"worker-fencing"}),
    "p5_worker_crash_redelivery": (".github/workflows/p5w2-worker-crash-redelivery-pg16.yml", {"worker-crash-redelivery"}),
    "p5_provider_ack": (".github/workflows/p5e-provider-ack-ambiguity-pg16.yml", {"provider-ack-ambiguity"}),
    "p5_dependency_loss": (".github/workflows/p5d-dependency-loss-pg16.yml", {"dependency-loss"}),
    "p5_race_deadlock": (".github/workflows/p5r-race-deadlock-pg16.yml", {"race-deadlock"}),
    "p5_compensation": (".github/workflows/p5c-compensation-crash-replay-pg16.yml", {"compensation-crash-replay"}),
}

HEAD_AWARE = {
    "p3",
    "p4b_evidence",
    "p4b_drift",
    "p4c_general",
    "p4c_notification",
    "p4d_refund",
    "p4e_contract",
    "p4e_operational",
    "p5_worker_fencing",
    "p5_worker_crash_redelivery",
    "p5_provider_ack",
    "p5_dependency_loss",
    "p5_race_deadlock",
    "p5_compensation",
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _matrix_entries(key: str) -> dict[str, tuple[str, set[str]]]:
    return {
        entry["id"]: (entry["workflow"], set(entry["required_jobs"]))
        for entry in _matrix()[key]
    }


def test_matrix_binds_certified_p5c_predecessor_and_current_head() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P5F"
    assert matrix["base"] == {
        "phase": "P5C",
        "commit": "4c21830edafda605b17194037ba0d78fe0492a86",
        "tree": "baef13057c8671e373255a9d9c2ab342e531b58d",
        "required_markers": [
            "P5C_BEFORE_COMMIT_CRASH=PASS",
            "P5C_REPLACEMENT_WORKER_RECOVERY=PASS",
            "P5C_AFTER_COMMIT_REDELIVERY=PASS",
            "P5C_SINGLE_COMPENSATION_EFFECT=PASS",
            "P5C_NO_FROZEN_UNRECOVERABLE_AGGREGATE=PASS",
        ],
    }
    assert matrix["candidate"] == {
        "branch": "hardening/p5f-final-certification-temp",
        "identity": "github.sha",
        "required_head_migration": HEAD,
        "immutability_rule": "any_candidate_change_invalidates_all_prior_decisive_results",
    }


def test_matrix_requires_complete_inherited_and_p5_topology() -> None:
    assert _matrix_entries("inherited_p1_p4_workflows") == INHERITED
    assert _matrix_entries("p5_workflows") == P5

    for workflow_path, expected_jobs in {**INHERITED, **P5}.values():
        path = ROOT / workflow_path
        assert path.is_file(), workflow_path
        workflow = _workflow(path)
        source = path.read_text(encoding="utf-8")
        assert "workflow_call" in workflow["on"], workflow_path
        assert set(workflow["jobs"]) == expected_jobs, workflow_path
        assert workflow["permissions"] == {"contents": "read"}, workflow_path
        assert "continue-on-error: true" not in source or (
            "Enforce general regression result" in source
        )


def test_final_workflow_calls_all_27_gates_on_same_head() -> None:
    workflow = _workflow(WORKFLOW_PATH)
    jobs = workflow["jobs"]
    expected = {**INHERITED, **P5}

    assert workflow["on"]["push"]["branches"] == [
        "hardening/p5f-final-certification-temp"
    ]
    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"contract", "certify", *expected}

    for job_id, (workflow_path, _expected_jobs) in expected.items():
        assert jobs[job_id]["uses"] == f"./{workflow_path}"
        if job_id in HEAD_AWARE:
            assert jobs[job_id]["with"] == {"certification_head": HEAD}
        else:
            assert "with" not in jobs[job_id]

    terminal = jobs["certify"]
    assert terminal["if"] == "${{ always() }}"
    assert set(terminal["needs"]) == {"contract", *expected}
    assert "continue-on-error" not in terminal

    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'details["result"] != "success"' in source
    assert "P5F_FINAL_SAME_HEAD_CERTIFICATION=PASS" in source
    assert "P5F_NO_LOST_UPDATES=PASS" in source
    assert "P5F_NO_DUPLICATE_FINANCIAL_EFFECTS=PASS" in source
    assert "P5F_NO_STUCK_TRANSITIONS=PASS" in source
    assert "P5F_NO_UNRECOVERABLE_JOBS=PASS" in source
    assert "P5F_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source


def test_current_head_inputs_are_explicit_and_reusable() -> None:
    for job_id in HEAD_AWARE:
        path = ROOT / ({**INHERITED, **P5}[job_id][0])
        workflow = _workflow(path)
        input_contract = workflow["on"]["workflow_call"]["inputs"]["certification_head"]
        assert input_contract["type"] == "string"
        assert input_contract["required"] in {"true", "false"}

    governance = _workflow(ROOT / P5["p5_governance"][0])
    assert "workflow_call" in governance["on"]


def test_p5f_keeps_governance_matrix_frozen_and_hard_stops_closed() -> None:
    frozen = (ROOT / "docs/architecture/p5_fault_injection_matrix.json").read_text(
        encoding="utf-8"
    )
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    matrix = _matrix()

    assert '"p5_certification": "not_yet_certified"' in frozen
    assert matrix["hard_gate_failures"] == [
        "lost_update",
        "duplicate_financial_effect",
        "stuck_transition",
        "unrecoverable_job",
    ]
    assert matrix["deferred_boundaries"] == {
        "refund_provider_execution": "deferred_fail_closed",
        "live_money_movement": False,
        "live_provider_credentials": False,
    }
    assert set(matrix["forbidden_actions"]) == {
        "merge",
        "retarget",
        "tag",
        "release",
        "deploy",
    }
    assert matrix["post_pass_state"] == (
        "separate_exact_candidate_merge_authorization_required"
    )
    for phrase in (
        "No live refund API call",
        "Telemetry, queue labels and operator input never become business authority",
        "does not perform or authorize a merge",
        "separate exact-candidate",
    ):
        assert phrase in contract


def test_p5f_p4e_wrappers_are_certification_only_and_current_head_bound() -> None:
    contract_source = (ROOT / INHERITED["p4e_contract"][0]).read_text(encoding="utf-8")
    operational_source = (ROOT / INHERITED["p4e_operational"][0]).read_text(encoding="utf-8")

    assert 'CERTIFICATION_HEAD: ${{ inputs.certification_head }}' in contract_source
    assert 'CERTIFICATION_HEAD: ${{ inputs.certification_head }}' in operational_source
    assert '= "${CERTIFICATION_HEAD}"' in contract_source
    assert '= "${CERTIFICATION_HEAD}"' in operational_source
    assert "tests/test_p4e_operational_snapshots_runtime.py" in operational_source
    assert "scripts/verify_head_workflow_bootstrap.py" in operational_source
    assert " BYPASSRLS" not in operational_source
    assert "P5F_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in contract_source
    assert "P5F_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in operational_source
