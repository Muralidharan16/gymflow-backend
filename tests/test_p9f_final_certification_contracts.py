from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P9F_FINAL_CERTIFICATION.md"
WORKFLOW = ROOT / ".github/workflows/p9f-final-same-head-certification.yml"
MATRIX = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"

P9D_HEAD = "63f927bef51c167d995d47ae6a65c458e38f0762"
P9D_TREE = "ce88550621b94e2ecdcae12e4143cae92ff19647"
ALEMBIC_HEAD = "zk07d8e9f0a45"
CERT_BRANCH = "hardening/p9-data-protection-disaster-recovery"
HARD_GATE = "upgrade_and_recovery_from_bad_deployment_or_data_loss_proven"

REQUIRED_INTERNAL_JOBS = ('contract', 'architecture', 'migration_hardening', 'finance', 'migration_lifecycle', 'migration_preservation', 'migration_adversarial', 'migration_contracts', 'migration_semantics', 'p3', 'p3a_general', 'lifecycle_maintenance', 'platform_maintenance', 'p4b_opensearch', 'p4b_evidence', 'p4b_drift', 'p4c_general', 'p4c_notification', 'p4d_refund', 'p4e_contract', 'p4e_operational', 'p5_governance', 'p5_worker_fencing', 'p5_worker_crash_redelivery', 'p5_provider_ack', 'p5_dependency_loss', 'p5_race_deadlock', 'p5_compensation', 'p6_governance', 'p6_redis', 'p6_broker', 'p6_worker', 'p6_poison', 'p6_scheduler', 'p7_governance', 'p7_system', 'p7_drain', 'p7_resource', 'p7_worker', 'p7_orchestration', 'p8_governance', 'p8_logging', 'p8_metrics', 'p8_alerts', 'p8_runbooks', 'p8_production_like')

P9_SAME_HEAD_WORKFLOWS = ('.github/workflows/p9-governance-data-protection-recovery.yml', '.github/workflows/p9m-populated-predecessor-head.yml', '.github/workflows/p9l-lock-rewrite-analysis.yml', '.github/workflows/p9b-backup-integrity.yml', '.github/workflows/p9r-full-restore-pitr.yml', '.github/workflows/p9c-rolling-schema-compatibility.yml', '.github/workflows/p9d-bad-deployment-rollback.yml')

FINAL_MARKERS = (
    "P9_GOVERNANCE_DATA_PROTECTION=PASS",
    "P9_POPULATED_PREDECESSOR_TO_HEAD=PASS",
    "P9_MIGRATION_DATA_INTEGRITY=PASS",
    "P9_LOCK_BUDGET=PASS",
    "P9_TABLE_REWRITE_ANALYSIS=PASS",
    "P9_MIGRATION_DURATION_MEASURED=PASS",
    "P9_LOGICAL_BACKUP_INTEGRITY=PASS",
    "P9_PHYSICAL_BASE_BACKUP=PASS",
    "P9_FULL_RESTORE=PASS",
    "P9_PITR=PASS",
    "P9_RPO_MEASURED=PASS",
    "P9_RTO_MEASURED=PASS",
    "P9_ROLLING_SCHEMA_COMPATIBILITY=PASS",
    "P9_DEPLOYMENT_ROLLBACK=PASS",
    "P9_BAD_DEPLOYMENT_RECOVERY=PASS",
    "P9_DATA_LOSS_RECOVERY=PASS",
    "P9_P1_P8_INHERITED=PASS",
    "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    "P9_UPGRADE_AND_RECOVERY_PROVEN=PASS",
    "P9F_FINAL_SAME_HEAD_CERTIFICATION=PASS",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.load(_read(WORKFLOW), Loader=yaml.BaseLoader)


def test_final_document_freezes_exact_p9d_parent_and_terminal_boundary() -> None:
    text = _read(DOC)
    assert P9D_HEAD in text
    assert P9D_TREE in text
    assert ALEMBIC_HEAD in text
    assert CERT_BRANCH in text
    assert HARD_GATE in text
    assert "exactly **53 prerequisite gates**" in text
    assert "46 inherited P1-P8" in text
    assert "seven P9 slice workflows" in text
    assert "merge to `main`" in text
    assert "Integration remains a separate explicitly authorized lifecycle step" in text


def test_workflow_is_read_only_and_bound_to_exact_p9d_parent() -> None:
    workflow = _workflow()
    text = _read(WORKFLOW)
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["on"]["push"]["branches"] == [CERT_BRANCH]
    assert workflow["env"]["P9D_CERTIFIED_HEAD"] == P9D_HEAD
    assert workflow["env"]["P9D_CERTIFIED_TREE"] == P9D_TREE
    assert workflow["env"]["P9_CURRENT_ALEMBIC_HEAD"] == ALEMBIC_HEAD
    assert workflow["env"]["P9_HARD_GATE"] == HARD_GATE
    assert 'git merge-base --is-ancestor "${P9D_CERTIFIED_HEAD}" HEAD' in text
    assert 'test "$(git rev-parse "${P9D_CERTIFIED_HEAD}^{tree}")" = "${P9D_CERTIFIED_TREE}"' in text
    assert 'test "$(git rev-parse HEAD)" = "${{ github.sha }}"' in text
    assert "git diff --exit-code" in text
    assert "git diff --cached --exit-code" in text


def test_final_topology_binds_46_inherited_and_seven_p9_same_head_gates() -> None:
    workflow = _workflow()
    text = _read(WORKFLOW)
    assert len(REQUIRED_INTERNAL_JOBS) == 46
    assert workflow["jobs"]["certify"]["needs"] == list(REQUIRED_INTERNAL_JOBS)
    assert "if len(results) != 46:" in text
    assert "if len(expected) != 7:" in text
    assert 'and run.get("head_sha") == sha' in text
    assert 'and run.get("event") == "push"' in text
    assert 'run["status"] != "completed"' in text
    assert 'run["conclusion"] != "success"' in text
    assert '"total_prerequisite_gate_count": len(inherited) + len(sibling)' in text
    assert 'assert decision["total_prerequisite_gate_count"] == 53' in text


def test_p9_slices_are_bound_by_sibling_actions_evidence_not_duplicate_calls() -> None:
    text = _read(WORKFLOW)
    for path in P9_SAME_HEAD_WORKFLOWS:
        assert f'"{path}"' in text
        assert f"uses: ./{path}" not in text
        reusable = _read(ROOT / path)
        assert "workflow_call:" in reusable
        assert "cancel-in-progress: true" in reusable


def test_contract_reproves_complete_p9_static_surface_and_p8_final_contract() -> None:
    text = _read(WORKFLOW)
    required_tests = (
        "tests/test_p8f_final_certification_contracts.py",
        "tests/test_p9_governance_contracts.py",
        "tests/test_p9m_populated_migration_contracts.py",
        "tests/test_p9l_lock_rewrite_contracts.py",
        "tests/test_p9b_backup_integrity_contracts.py",
        "tests/test_p9r_full_restore_pitr_contracts.py",
        "tests/test_p9r_recovery_compatibility_contracts.py",
        "tests/test_p9c_rolling_schema_compatibility_contracts.py",
        "tests/test_p9d_bad_deployment_rollback_contracts.py",
        "tests/test_p9f_final_certification_contracts.py",
    )
    for test_path in required_tests:
        assert test_path in text
    assert "scripts/verify_alembic_graph.py" in text
    assert "scripts/verify_head_workflow_bootstrap.py" in text
    assert "docs/architecture/p9_data_protection_recovery_matrix.json" in text


def test_terminal_markers_match_frozen_p9_acceptance_contract() -> None:
    text = _read(WORKFLOW)
    for marker in FINAL_MARKERS:
        assert f'echo "{marker}"' in text
    assert 'matrix["hard_gate"] == "upgrade_and_recovery_from_bad_deployment_or_data_loss_proven"' in text
    assert '"refund_provider_execution": matrix["authority"]["refund_provider_execution"]' in text
    assert '"durable_business_work_authority": matrix["authority"]["durable_business_work"]' in text


def test_final_workflow_has_no_soft_failure_or_integration_action() -> None:
    text = _read(WORKFLOW)
    assert "continue-on-error" not in text
    assert "pulls: write" not in text
    assert "contents: write" not in text
    assert "actions: write" not in text
    assert "gh pr merge" not in text
    assert "git push origin main" not in text
    assert "git tag " not in text
    assert "gh release" not in text
    assert "kubectl apply" not in text
    assert "helm upgrade" not in text
    assert "alembic downgrade" not in text


def test_terminal_evidence_is_machine_readable_and_exact_sha_bound() -> None:
    text = _read(WORKFLOW)
    assert 'Path("p9f-evidence/inherited-needs.json")' in text
    assert 'Path("p9f-evidence/p9-same-head-runs.json")' in text
    assert 'Path("p9f-evidence/decision.json")' in text
    assert "p9f-final-${{ github.sha }}" in text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    assert '"candidate_sha": os.environ["GITHUB_SHA"]' in text
    assert '"decision": "PASS"' in text
