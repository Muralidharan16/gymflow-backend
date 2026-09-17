from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
SCOPE = ROOT / "docs/architecture/P9M_POPULATED_PREDECESSOR_HEAD_REHEARSAL.md"
WORKFLOW = ROOT / ".github/workflows/p9m-populated-predecessor-head.yml"
SEED = ROOT / "scripts/ci/p9m_seed_populated_predecessor.sql"
SNAPSHOT = ROOT / "scripts/ci/p9m_stable_snapshot.sql"
VERIFY = ROOT / "scripts/ci/p9m_verify_head_capability.sql"
TIMER = ROOT / "scripts/p9m_upgrade_timer.py"

P9_BRANCH = "hardening/p9-data-protection-disaster-recovery"
PREDECESSOR = "zj07d8e9f0a44"
HEAD = "zk07d8e9f0a45"

MARKERS = {
    "P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS",
    "P9M_PREDECESSOR_CAPTURE=PASS",
    "P9M_EXACT_FORWARD_UPGRADE=PASS",
    "P9M_STABLE_FINGERPRINT=PASS",
    "P9M_EXPECTED_CAPABILITY_DELTA=PASS",
    "P9M_UPGRADE_DURATION_RECORDED=PASS",
    "P9_POPULATED_PREDECESSOR_TO_HEAD=PASS",
    "P9_MIGRATION_DATA_INTEGRITY=PASS",
    "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
}


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_p9m_files_and_scope_freeze_exact_migration_boundary() -> None:
    for path in (MATRIX, SCOPE, WORKFLOW, SEED, SNAPSHOT, VERIFY, TIMER):
        assert path.is_file(), path
    scope = SCOPE.read_text(encoding="utf-8")
    assert PREDECESSOR in scope
    assert HEAD in scope
    assert "real PostgreSQL 16" in scope
    assert "No production/customer backup" in scope
    assert "64 organizations" in scope
    assert "192 branches" in scope
    assert "4096 durable branch-outbox rows" in scope
    assert "exactly 204 dead-lettered `branch.lifecycle_saga` rows" in scope
    for marker in MARKERS:
        assert marker in scope


def test_p9m_seed_is_large_deterministic_tenant_bound_and_synthetic_only() -> None:
    source = SEED.read_text(encoding="utf-8")
    assert "generate_series(1, 64)" in source
    assert "FOR org_sequence IN 1..64 LOOP" in source
    assert "FOR branch_sequence IN 1..3 LOOP" in source
    assert "pg_catalog.set_config('app.current_org_id', org_id::text, true)" in source
    assert "generate_series(1, 4096)" in source
    assert "p9m_synthetic" in source
    assert "branch.lifecycle_saga" in source
    assert "dead_lettered" in source
    assert "'delivered'" in source
    assert "dead_lifecycle_count <> 204" in source
    assert "P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS" in source
    lowered = source.lower()
    assert "disable trigger" not in lowered
    assert "session_replication_role" not in lowered
    assert "pg_restore" not in lowered
    assert "aws s3" not in lowered
    assert "production backup" not in lowered


def test_p9m_snapshot_covers_business_security_and_authority_surfaces() -> None:
    source = SNAPSHOT.read_text(encoding="utf-8")
    for token in (
        "organizations|",
        "branches|",
        "branch_outbox|",
        "outbox_status_distribution|",
        "outbox_table_acl|",
        "outbox_column_acl|",
        "critical_relation_security|",
        "critical_role_posture|",
        "app_security_owner_raw_outbox_select|",
        "lifecycle_maintenance_raw_outbox_select|",
        "dead_lifecycle_count|",
    ):
        assert token in source
    assert "relrowsecurity" in source
    assert "relforcerowsecurity" in source
    assert "rolbypassrls" in source


def test_p9m_timer_uses_monotonic_clock_and_exact_revisions() -> None:
    source = TIMER.read_text(encoding="utf-8")
    assert f'EXPECTED_PREDECESSOR = "{PREDECESSOR}"' in source
    assert f'EXPECTED_HEAD = "{HEAD}"' in source
    assert "time.monotonic_ns()" in source
    assert '"clock": "time.monotonic_ns"' in source
    assert "P9M_UPGRADE_DURATION_RECORDED=PASS" in source
    assert "alembic" in source and "upgrade" in source


def test_p9m_head_verification_proves_only_bounded_maintenance_capability() -> None:
    source = VERIFY.read_text(encoding="utf-8")
    assert "lifecycle_saga_dead_letter_count" in source
    assert "app_security_owner" in source
    assert "SECURITY DEFINER" in source
    assert "row_security=on" in source
    assert "search_path=%" in source
    assert "PUBLIC EXECUTE" in source
    for role in ("app_runtime", "auth_runtime", "worker_runtime", "finance_config_runtime"):
        assert role in source
    assert "lifecycle_maintenance_runtime" in source
    assert "unexpectedly read raw outbox rows" in source
    assert "observed <> 204" in source
    assert "P9M_EXPECTED_CAPABILITY_DELTA=PASS" in source


def test_p9m_workflow_is_exact_branch_real_pg16_and_fail_closed() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P9_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"populated_rehearsal"}
    job = workflow["jobs"]["populated_rehearsal"]
    assert job["env"]["P9M_PREDECESSOR"] == PREDECESSOR
    assert job["env"]["P9M_HEAD"] == HEAD
    assert "postgresql-16" in source
    assert "scripts/ci/bootstrap_cluster_roles.sh" in source
    assert f"upgrade \"${{P9M_PREDECESSOR}}\"" in source
    assert "p9m_seed_populated_predecessor.sql" in source
    assert "p9m_stable_snapshot.sql" in source
    assert "p9m_upgrade_timer.py" in source
    assert 'cmp "$EVIDENCE_DIR/predecessor-stable.txt" "$EVIDENCE_DIR/head-stable.txt"' in source
    assert "p9m_verify_head_capability.sql" in source
    assert "tests/test_p9_governance_contracts.py" in source
    assert "tests/test_p8_governance_contracts.py" in source
    assert "tests/test_migration_app_secure_owner_context_boundary.py" in source
    for marker in MARKERS:
        assert marker in source or marker == "P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS" or marker == "P9M_EXPECTED_CAPABILITY_DELTA=PASS" or marker == "P9M_UPGRADE_DURATION_RECORDED=PASS"
    assert "alembic downgrade" not in source
    assert "production deployment" not in source.lower()
