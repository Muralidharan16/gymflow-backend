from __future__ import annotations

from pathlib import Path


MIGRATION = Path(
    "alembic/versions/c87d8e9f0a22_p3a_auth_initial_branch_state_rls.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_c87_extends_c77_as_the_next_p3a_head() -> None:
    source = _source()
    assert 'revision = "c87d8e9f0a22"' in source
    assert 'down_revision = "c77d8e9f0a21"' in source


def test_c87_changes_rls_only_and_does_not_widen_auth_acl() -> None:
    source = _source()
    assert '_EXPECTED_TABLE_ACL = {"INSERT"}' in source
    assert '("status_changed_at", "SELECT", False, _MIGRATION_OWNER)' in source
    assert '("updated_at", "SELECT", False, _MIGRATION_OWNER)' in source
    assert "broad org_branch_state SELECT" in source
    assert 'forbidden in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")' in source
    assert "GRANT SELECT" not in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source
    assert "GRANT INSERT" not in source


def test_auth_bootstrap_policy_is_bound_to_real_db_identity_and_tenant() -> None:
    source = _source()
    assert "current_user = session_user" in source
    assert "pg_catalog.pg_has_role(session_user, 'auth_runtime', 'MEMBER')" in source
    assert "auth.role() = 'owner'" in source
    assert "app.current_org_id" in source
    assert "app.current_user_id" in source
    assert "app.current_principal_type" in source
    assert "app.current_gym_id" in source
    assert "('owner', 'legacy_gym_owner')" in source
    assert "app_runtime" not in source.split("_FORWARD_POLICY =", 1)[1].split(
        "_PREDECESSOR_POLICY =", 1
    )[0]


def test_auth_bootstrap_policy_only_accepts_canonical_initial_state() -> None:
    source = _source()
    required_fragments = (
        "branch_status = 'active'",
        "is_primary IS TRUE",
        "is_active IS TRUE",
        "is_public IS TRUE",
        "status = 'active'",
        "is_operational IS TRUE",
        "status_changed_by IS NULL",
        "status_reason IS NULL",
        "transition_source = 'api'",
        "scheduled_transition_at IS NULL",
        "scheduled_transition_to IS NULL",
        "lifecycle_transition_in_progress IS FALSE",
        "saga_last_checkpoint IS NULL",
        "saga_compensation_strategy IS NULL",
        "watchdog_recovered_at IS NULL",
        "watchdog_recovery_count = 0",
        "search_visibility_version = 1",
        "search_last_synced_at IS NULL",
        "search_sync_failed_at IS NULL",
        "reconciliation_claimed_by IS NULL",
        "reconciliation_claimed_at IS NULL",
        "worm_archive_uri IS NULL",
        "worm_archive_checksum IS NULL",
        "worm_archive_verified_at IS NULL",
        "worm_archive_status IS NULL",
        "version = 1",
        "search_logical_clock = 0",
        "deleted_at IS NULL",
        "archived_at IS NULL",
        "purged_at IS NULL",
        "pg_catalog.char_length(search_epoch_ulid) = 26",
        "^[0-9A-HJKMNP-TV-Z]{26}$",
    )
    for fragment in required_fragments:
        assert fragment in source


def test_df590_superadmin_system_path_is_preserved_without_ordinary_owner_widening() -> None:
    source = _source()
    forward = source.split("_FORWARD_POLICY =", 1)[1].split(
        "_PREDECESSOR_POLICY =", 1
    )[0]
    assert "auth.role() IN ('superadmin', 'system')" in forward
    assert forward.count("auth.role() = 'owner'") == 1
    assert "auth.role() IN ('superadmin', 'system', 'owner')" not in forward
    assert "auth.role() IN ('owner', 'superadmin', 'system')" not in forward


def test_c87_downgrade_restores_exact_predecessor_policy_contract() -> None:
    source = _source()
    predecessor = source.split("_PREDECESSOR_POLICY =", 1)[1]
    assert "auth.role() IN ('superadmin', 'system')" in predecessor
    assert "auth_runtime" not in predecessor.split('"""', 2)[1]
    assert source.count("_require_predecessor(bind)") >= 2
    assert source.count("_require_forward(bind)") >= 2
