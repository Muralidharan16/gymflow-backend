from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
WORKFLOW_PATH = ROOT / ".github/workflows/p9c-rolling-schema-compatibility.yml"
SCRIPT_PATH = ROOT / "scripts/ci/run_p9c_rolling_schema_compatibility.sh"
MIGRATION_PATH = ROOT / "alembic/versions/zk07d8e9f0a45_p8_lifecycle_dead_letter_snapshot.py"

OLD_APPLICATION_SHA = "14c05f7afaccebde0c6975c7c9c41dc70b41c805"
PREDECESSOR = "zj07d8e9f0a44"
HEAD = "zk07d8e9f0a45"
BRANCH = "hardening/p9-data-protection-disaster-recovery"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _source() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8") + "\n" + SCRIPT_PATH.read_text(encoding="utf-8")


def test_p9c_matrix_requires_true_rolling_overlap_and_app_rollback() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    compatibility = matrix["rolling_schema_compatibility"]
    assert compatibility == {
        "old_application_on_upgraded_schema_required": True,
        "new_application_on_upgraded_schema_required": True,
        "old_and_new_application_overlap_required": True,
        "rollback_application_on_upgraded_schema_required": True,
        "destructive_contract_migration_during_overlap_forbidden": True,
        "expand_migrate_contract_discipline_required": True,
    }


def test_p9c_workflow_is_exact_revision_and_real_pg16_bound() -> None:
    workflow = _workflow()
    source = _source()
    job = workflow["jobs"]["rolling_compatibility"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [BRANCH]
    assert "workflow_call" in workflow["on"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["env"]["P9C_OLD_APP_SHA"] == OLD_APPLICATION_SHA
    assert job["env"]["P9C_PREDECESSOR"] == PREDECESSOR
    assert job["env"]["P9C_HEAD"] == HEAD
    assert "postgresql-16" in source
    assert "git worktree add --detach" in source
    assert 'test "$old_head" = "$P9C_PREDECESSOR"' in source
    assert 'test "$new_head" = "$P9C_HEAD"' in source
    assert "cmp requirements-test.lock" in source


def test_p9c_uses_one_forward_upgraded_database_and_never_database_downgrade() -> None:
    source = _source().lower()
    assert 'upgrade "$p9c_predecessor"' in source
    assert 'upgrade "$p9c_head"' in source
    assert "alembic downgrade" not in source
    assert "'database_downgrade_used': false" in source
    assert "p9c_forward_only_schema_upgrade=pass" in source


def test_p9c_proves_old_new_concurrent_readiness_and_exact_old_restart() -> None:
    source = _source()
    assert 'launch_app "$P9C_OLD_RUNTIME"' in source
    assert 'launch_app "$P9C_NEW_RUNTIME"' in source
    assert "P9C_OLD_APPLICATION_ON_UPGRADED_SCHEMA=PASS" in source
    assert "P9C_NEW_APPLICATION_ON_UPGRADED_SCHEMA=PASS" in source
    assert "P9C_OLD_NEW_APPLICATION_OVERLAP=PASS" in source
    assert "P9C_APPLICATION_ROLLBACK_ON_UPGRADED_SCHEMA=PASS" in source
    assert "old-app-rollback-restart.log" in source
    assert "old_new_overlap_probe_count" in source
    assert "/_system/ready" in source


def test_p9c_preserves_data_security_and_provider_fail_closed_boundaries() -> None:
    source = _source()
    for required in (
        "p9m_seed_populated_predecessor.sql",
        "p9m_stable_snapshot.sql",
        "p9m_verify_head_capability.sql",
        "P9C_POST_ROLLBACK_DATA_SECURITY_INTEGRITY=PASS",
        "P9C_LIVE_PROVIDER_EGRESS_BLOCKED=PASS",
        "NOTIFICATION_EMAIL_PROVIDER_MODE=disabled",
        "SEARCH_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_PROVIDER_MODE=disabled",
        "PLATFORM_BILLING_CHECKOUT=false",
        "PLATFORM_BILLING_WEBHOOK_PROCESSING=false",
        "PLATFORM_BILLING_DUNNING_TRANSITIONS=false",
        "PLATFORM_BILLING_NOTIFICATIONS=false",
        "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert required in source


def test_p9c_zk07_overlap_migration_is_additive_contract_only() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8").lower()
    assert 'revision = "zk07d8e9f0a45"' in migration
    assert 'down_revision = "zj07d8e9f0a44"' in migration
    assert "create function app_secure.lifecycle_saga_dead_letter_count()" in migration
    assert "security definer" in migration
    assert "drop table" not in migration
    assert "drop column" not in migration
    assert "op.drop_table" not in migration
    assert "op.drop_column" not in migration


def test_p9c_emits_machine_readable_evidence_and_terminal_marker() -> None:
    source = _source()
    assert "decision.json" in source
    assert "'decision': 'PASS'" in source
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in source
    assert "P9_ROLLING_SCHEMA_COMPATIBILITY=PASS" in source
