from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
SCOPE_PATH = ROOT / "docs/architecture/P9_DATA_PROTECTION_RECOVERY_SCOPE_AND_GATES.md"
ACCEPTANCE_PATH = ROOT / "docs/architecture/P9_ACCEPTANCE_MATRIX.md"
WORKFLOW_PATH = ROOT / ".github/workflows/p9-governance-data-protection-recovery.yml"

P8_MERGE_BASE = "8e0d6b66278e187209e6b029e11f24e475c6a24f"
P8_CERTIFIED_TREE = "199b87b7dd8f65ed4c4f3303a01450ef9f8ec604"
P9_BRANCH = "hardening/p9-data-protection-disaster-recovery"
ALEMBIC_HEAD = "zk07d8e9f0a45"
ALEMBIC_PREDECESSOR = "zj07d8e9f0a44"

FINAL_MARKERS = {
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
}


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p9_binds_exact_merged_p8_baseline_and_schema_boundary() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == 1
    assert matrix["phase"] == "P9"
    assert matrix["base_commit"] == P8_MERGE_BASE
    assert matrix["base_tree"] == P8_CERTIFIED_TREE
    assert matrix["branch"] == P9_BRANCH
    assert matrix["alembic_head"] == ALEMBIC_HEAD
    assert matrix["alembic_predecessor"] == ALEMBIC_PREDECESSOR
    assert matrix["slices"] == ["P9-G", "P9-M", "P9-L", "P9-B", "P9-R", "P9-C", "P9-D", "P9-F"]
    assert matrix["hard_gate"] == "upgrade_and_recovery_from_bad_deployment_or_data_loss_proven"


def test_p9_requires_real_populated_predecessor_to_head_rehearsal() -> None:
    contract = _matrix()["migration_rehearsal"]
    assert contract["production_shaped_populated_predecessor_required"] is True
    assert contract["predecessor_to_head_required"] is True
    assert contract["row_and_business_invariant_preservation_required"] is True
    assert contract["security_rls_acl_preservation_required"] is True
    assert contract["schema_and_data_post_upgrade_validation_required"] is True
    assert contract["repeatability_required"] is True
    assert contract["real_postgresql_16_required"] is True
    assert contract["mock_only_proof_forbidden"] is True


def test_p9_freezes_lock_rewrite_and_duration_evidence() -> None:
    contract = _matrix()["lock_and_rewrite_analysis"]
    assert all(contract.values())
    assert contract["unexpected_access_exclusive_lock_is_hard_stop"] is True
    assert contract["unbounded_lock_wait_is_hard_stop"] is True


def test_p9_backup_success_requires_restoreability() -> None:
    contract = _matrix()["backup_integrity"]
    assert contract["logical_backup_required"] is True
    assert contract["logical_backup_checksum_required"] is True
    assert contract["logical_backup_catalog_validation_required"] is True
    assert contract["physical_base_backup_required_for_pitr"] is True
    assert contract["wal_archive_required_for_pitr"] is True
    assert contract["backup_restoreability_is_required_for_success"] is True
    assert contract["backup_creation_alone_is_not_success"] is True
    assert contract["real_customer_data_in_ci_forbidden"] is True


def test_p9_full_restore_and_pitr_require_data_boundary_proof() -> None:
    contract = _matrix()["restore_and_pitr"]
    assert contract["full_restore_into_isolated_cluster_required"] is True
    assert contract["restored_schema_validation_required"] is True
    assert contract["restored_business_invariant_validation_required"] is True
    assert contract["restored_security_rls_acl_validation_required"] is True
    assert contract["point_in_time_recovery_required"] is True
    assert contract["pre_loss_durable_record_must_survive"] is True
    assert contract["post_target_record_must_not_survive"] is True
    assert contract["recovery_timeline_evidence_required"] is True


def test_p9_requires_measured_rpo_rto_and_frozen_targets_before_recovery_certification() -> None:
    objectives = _matrix()["recovery_objectives"]
    assert objectives["rpo_must_be_measured"] is True
    assert objectives["rto_must_be_measured"] is True
    assert objectives["measurement_clock_must_be_monotonic_for_duration"] is True
    assert objectives["targets_must_be_frozen_before_p9_r_certification"] is True
    assert objectives["measured_values_must_be_recorded_as_evidence"] is True
    assert "recovery_target" in objectives["rpo_definition"]
    assert "readiness" in objectives["rto_definition"]


def test_p9_requires_rolling_overlap_and_rollback_on_upgraded_schema() -> None:
    compatibility = _matrix()["rolling_schema_compatibility"]
    assert compatibility["old_application_on_upgraded_schema_required"] is True
    assert compatibility["new_application_on_upgraded_schema_required"] is True
    assert compatibility["old_and_new_application_overlap_required"] is True
    assert compatibility["rollback_application_on_upgraded_schema_required"] is True
    assert compatibility["destructive_contract_migration_during_overlap_forbidden"] is True
    assert compatibility["expand_migrate_contract_discipline_required"] is True


def test_p9_bad_deployment_rollback_preserves_durable_and_financial_effects() -> None:
    rollback = _matrix()["deployment_rollback"]
    assert rollback["bad_release_injection_required"] is True
    assert rollback["readiness_failure_or_runtime_failure_detection_required"] is True
    assert rollback["traffic_return_to_last_known_good_required"] is True
    assert rollback["database_downgrade_must_not_be_first_line_rollback"] is True
    assert rollback["inflight_and_durable_work_recovery_required"] is True
    assert rollback["post_rollback_integrity_validation_required"] is True
    assert rollback["no_duplicate_financial_effects_required"] is True
    assert rollback["no_lost_durable_work_required"] is True


def test_p9_preserves_authority_and_hard_stops() -> None:
    matrix = _matrix()
    assert matrix["authority"] == {
        "durable_business_work": "postgresql",
        "redis_celery_beat": "delivery_coordination_only",
        "refund_provider_execution": "deferred_fail_closed",
        "backup_or_restore_artifact_is_not_live_business_authority": True,
        "production_data_copy_to_ci": "forbidden",
    }
    assert set(matrix["hard_stops"]) == {
        "p1_p8_security_finance_runtime_or_observability_semantic_weakening",
        "real_customer_data_copied_into_ci_or_uncontrolled_rehearsal",
        "backup_reported_success_without_restoreability_proof",
        "pitr_without_pre_and_post_target_data_boundary_proof",
        "unbounded_or_unexplained_migration_lock_wait",
        "unexpected_table_rewrite_without_explicit_review",
        "rolling_deploy_requires_destructive_schema_change_before_old_version_drains",
        "rollback_depends_on_immediate_destructive_database_downgrade",
        "recovery_loses_committed_data_outside_frozen_rpo",
        "recovery_exceeds_frozen_rto",
        "restored_environment_can_accidentally_contact_live_providers",
        "database_credential_or_runtime_privilege_broadening",
        "refund_provider_activation",
        "live_money_movement",
        "release",
        "production_deployment",
    }


def test_p9_acceptance_locks_terminal_markers_and_decisive_language() -> None:
    acceptance = ACCEPTANCE_PATH.read_text(encoding="utf-8")
    scope = " ".join(SCOPE_PATH.read_text(encoding="utf-8").split())
    for marker in FINAL_MARKERS:
        assert marker in acceptance
    for phrase in (
        "Backup creation alone is not success",
        "Mock-only migration proof is forbidden",
        "pre-target sentinel",
        "post-target sentinel",
        "Numeric RPO/RTO targets must be frozen before P9-R can certify",
        "old application version must remain functional on the upgraded schema",
        "immediate destructive downgrade",
        "safe forward upgrade and recovery from a bad deployment or data-loss event",
    ):
        assert phrase in scope


def test_p9_governance_workflow_is_exact_base_branch_and_head_bound() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P9_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"governance"}
    job = workflow["jobs"]["governance"]
    assert job["env"]["P8_MERGE_BASE"] == P8_MERGE_BASE
    assert job["env"]["P8_CERTIFIED_TREE"] == P8_CERTIFIED_TREE
    assert job["env"]["P9_ALEMBIC_HEAD"] == ALEMBIC_HEAD
    assert job["env"]["P9_ALEMBIC_PREDECESSOR"] == ALEMBIC_PREDECESSOR
    assert "git merge-base --is-ancestor \"${P8_MERGE_BASE}\" HEAD" in source
    assert "tests/test_p9_governance_contracts.py" in source
    assert "tests/test_p8f_final_certification_contracts.py" in source
    assert "P9_GOVERNANCE_DATA_PROTECTION=PASS" in source
    assert "P9_UPGRADE_AND_RECOVERY_PROVEN=GOVERNANCE_FROZEN" in source
    assert "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED" in source
