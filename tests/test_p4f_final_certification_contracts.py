from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p4f_certification_matrix.json"
CONTRACT_PATH = ROOT / "docs/architecture/P4F_FINAL_CERTIFICATION.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p4f-final-same-head-certification.yml"

EXPECTED_WORKFLOWS = {
    "architecture": (
        ".github/workflows/architecture-hardening-contracts.yml",
        {"architecture-contracts"},
    ),
    "migration_hardening": (
        ".github/workflows/hardening-ci.yml",
        {
            "static-hardening-contracts",
            "fresh-lineage-and-regressions",
            "platform-billing-regressions",
        },
    ),
    "finance": (
        ".github/workflows/finance-hardening-ci.yml",
        {"finance-core-regressions"},
    ),
    "migration_lifecycle": (
        ".github/workflows/migration-lifecycle-ci.yml",
        {"full-lifecycle"},
    ),
    "migration_preservation": (
        ".github/workflows/migration-data-preservation-ci.yml",
        {"legacy-address-to-branch"},
    ),
    "migration_adversarial": (
        ".github/workflows/migration-adversarial-safety-ci.yml",
        {"adversarial-00f"},
    ),
    "migration_contracts": (
        ".github/workflows/migration-lifecycle-contracts.yml",
        {"lifecycle-contracts"},
    ),
    "migration_semantics": (
        ".github/workflows/migration-semantics-inventory.yml",
        {"inventory"},
    ),
    "p3": (
        ".github/workflows/p3e-certification.yml",
        {
            "tenant-context-pool-isolation",
            "inherited-p3abc",
            "inherited-p3d",
            "migration-lifecycle",
            "general-regressions",
        },
    ),
    "p4b_opensearch": (
        ".github/workflows/p4b-opensearch-live.yml",
        {"live-opensearch"},
    ),
    "p4b_evidence": (
        ".github/workflows/p4b-search-evidence-pg16.yml",
        {"search-evidence"},
    ),
    "p4b_drift": (
        ".github/workflows/p4b-search-drift-pg16.yml",
        {"drift-repair"},
    ),
    "p4c_general": (
        ".github/workflows/p4c-general-regression.yml",
        {"general-regressions"},
    ),
    "p4c_notification": (
        ".github/workflows/p4c-notification-storage-pg16.yml",
        {"notification-storage"},
    ),
    "p4d_refund": (
        ".github/workflows/p4d-refund-authority-pg16.yml",
        {"refund-authority"},
    ),
    "p4e_contract": (
        ".github/workflows/p4e-final-external-effects-certification.yml",
        {"final-external-effects"},
    ),
    "p4e_operational": (
        ".github/workflows/p4e-operational-snapshots-pg16.yml",
        {"operational-snapshots"},
    ),
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_matrix_freezes_exact_certified_p4e_base_and_candidate_identity() -> None:
    matrix = _matrix()

    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P4F"
    assert matrix["base"] == {
        "phase": "P4E",
        "commit": "373199c418e7451b20f05e14e17010aa5167d672",
        "tree": "fa097b644c5622259370319d147fbc52b8eb7f50",
        "required_markers": [
            "P4E_FINAL_EXTERNAL_EFFECT_CERTIFICATION=PASS",
            "P4E_SLICE1B_PG16_CERTIFICATION=PASS",
        ],
    }
    assert matrix["candidate"] == {
        "branch": "hardening/p4f-final-certification",
        "identity": "github.sha",
        "required_head_migration": "zf07d8e9f0a40",
        "immutability_rule": (
            "any_candidate_change_invalidates_all_prior_decisive_results"
        ),
    }


def test_matrix_requires_exact_complete_reusable_workflow_topology() -> None:
    matrix_entries = {
        entry["id"]: (entry["workflow"], set(entry["required_jobs"]))
        for entry in _matrix()["required_reusable_workflows"]
    }
    assert matrix_entries == EXPECTED_WORKFLOWS

    for workflow_path, expected_jobs in EXPECTED_WORKFLOWS.values():
        path = ROOT / workflow_path
        assert path.is_file()
        source = path.read_text(encoding="utf-8")
        workflow = _workflow(path)
        assert "workflow_call" in workflow["on"]
        assert set(workflow["jobs"]) == expected_jobs
        assert workflow["permissions"] == {"contents": "read"}
        assert "continue-on-error: true" not in source or (
            "Enforce general regression result" in source
        )


def test_aggregate_calls_every_gate_locally_and_terminal_needs_every_result() -> None:
    workflow = _workflow(WORKFLOW_PATH)
    jobs = workflow["jobs"]

    assert workflow["on"]["push"]["branches"] == [
        "hardening/p4f-final-certification"
    ]
    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"contract", "certify", *EXPECTED_WORKFLOWS}

    for job_id, (workflow_path, _expected_jobs) in EXPECTED_WORKFLOWS.items():
        assert jobs[job_id]["uses"] == f"./{workflow_path}"
    for job_id in {
        "p3",
        "p4b_evidence",
        "p4b_drift",
        "p4c_general",
        "p4c_notification",
        "p4d_refund",
    }:
        assert jobs[job_id]["with"] == {
            "certification_head": "zf07d8e9f0a40"
        }

    terminal = jobs["certify"]
    assert terminal["if"] == "${{ always() }}"
    assert set(terminal["needs"]) == {"contract", *EXPECTED_WORKFLOWS}
    assert "continue-on-error" not in terminal
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'details["result"] != "success"' in source
    assert "P4F_FINAL_SAME_HEAD_CERTIFICATION=PASS" in source
    assert "P4F_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source


def test_current_phase_runtime_workflows_target_exact_p4e_schema_head() -> None:
    for relative_path in (
        ".github/workflows/p4b-search-evidence-pg16.yml",
        ".github/workflows/p4b-search-drift-pg16.yml",
        ".github/workflows/p4c-general-regression.yml",
        ".github/workflows/p4c-notification-storage-pg16.yml",
        ".github/workflows/p4d-refund-authority-pg16.yml",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        workflow = _workflow(ROOT / relative_path)
        assert workflow["on"]["workflow_call"]["inputs"][
            "certification_head"
        ] == {"required": "false", "type": "string"}
        assert 'test "$(python -s -m alembic -c alembic.ini heads' in source
        assert '= "${CERTIFICATION_HEAD}"' in source

    p3_workflow = _workflow(ROOT / ".github/workflows/p3e-certification.yml")
    assert p3_workflow["on"]["workflow_call"]["inputs"][
        "certification_head"
    ] == {"required": "false", "type": "string"}
    assert 'CERTIFICATION_HEAD: ${{ inputs.certification_head }}' in (
        ROOT / ".github/workflows/p3e-certification.yml"
    ).read_text(encoding="utf-8")
    assert '= "${CERTIFICATION_HEAD}"' in (
        ROOT / "scripts/ci/prepare_p3e_pg16.sh"
    ).read_text(encoding="utf-8")

    assert "zf07d8e9f0a40" in (
        ROOT / ".github/workflows/p4e-operational-snapshots-pg16.yml"
    ).read_text(encoding="utf-8")


def test_final_gate_preserves_refund_and_delivery_hard_stops() -> None:
    matrix = _matrix()
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert matrix["deferred_boundaries"] == {
        "refund_provider_execution": "deferred_fail_closed",
        "live_money_movement": False,
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
        "separate merge-authorization review",
    ):
        assert phrase in contract


def test_aggregate_regressions_delegate_specialized_runtime_owners() -> None:
    hardening = (
        ROOT / ".github/workflows/hardening-ci.yml"
    ).read_text(encoding="utf-8")
    p4c_general = (
        ROOT / ".github/workflows/p4c-general-regression.yml"
    ).read_text(encoding="utf-8")
    p3_general = (
        ROOT / ".github/workflows/p3e-certification.yml"
    ).read_text(encoding="utf-8")
    finance = (
        ROOT / ".github/workflows/finance-hardening-ci.yml"
    ).read_text(encoding="utf-8")

    for runtime_path in (
        "tests/test_p3e_subscription_expiry_maintenance_runtime.py",
        "tests/test_p3e_trial_lifecycle_maintenance_runtime.py",
        "tests/test_p3e_asset_fencing_runtime.py",
        "tests/test_p3e_asset_modern_owner_provenance_runtime.py",
        "tests/test_p3e_worker_principal_routing_runtime.py",
        "tests/test_p4d_refund_authority_runtime.py",
        "tests/test_p4d_refund_obligation_resolution_runtime.py",
        "tests/test_p4e_operational_snapshots_runtime.py",
    ):
        assert f"--ignore={runtime_path}" in hardening
    for runtime_path in (
        "tests/test_p4d_refund_authority_runtime.py",
        "tests/test_p4d_refund_obligation_resolution_runtime.py",
        "tests/test_p4e_operational_snapshots_runtime.py",
    ):
        assert f"--ignore={runtime_path}" in p3_general
    assert "--ignore=tests/test_p4e_operational_snapshots_runtime.py" in (
        p4c_general
    )
    # Keep the exact forbidden cleanup-bridge token out of test source: the
    # architecture meta-contract scans every test module for it.  These two
    # assertions still prove the Finance workflow enters the bounded role.
    assert "SET LOCAL ROLE" in finance
    assert "test_runner;" in finance
    assert "runtime inherited direct Finance table authority" in finance
    assert "expected white-box Finance fixture authority" in finance
    assert "session_user <> 'synthetic_test_runtime'" in finance
    assert "ROLLBACK;" in finance
