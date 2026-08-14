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


def test_c87_preserves_exact_auth_acl_and_does_not_widen_privileges() -> None:
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
    assert "GRANT auth_runtime" not in source


def test_c87_adds_only_auth_returning_select_policy() -> None:
    source = _source()
    assert '_INSERT_POLICY = "p_branch_insert"' in source
    assert '_SELECT_POLICY = "p_branch_select"' in source
    assert '_RETURNING_POLICY = "p_branch_select_auth_bootstrap_returning"' in source
    assert "CREATE POLICY p_branch_select_auth_bootstrap_returning" in source
    assert "ON public.org_branch_state" in source
    assert "AS PERMISSIVE" in source
    assert "FOR SELECT" in source
    assert "TO auth_runtime" in source
    assert "FOR INSERT\nTO auth_runtime" not in source
    assert "p_branch_insert_auth_bootstrap" not in source
    assert "ALTER POLICY p_branch_insert" not in source
    assert "auth RETURNING policy must target exactly auth_runtime" in source


def test_c87_proves_predecessor_insert_and_select_policies_are_unchanged() -> None:
    source = _source()
    assert "predecessor_before = _require_predecessor_policies(bind)" in source
    assert "predecessor_after = _require_predecessor_policies(bind)" in source
    assert source.count("if predecessor_after != predecessor_before:") == 2
    assert "c87 changed predecessor branch-state policies" in source
    assert "c87 downgrade changed predecessor branch-state policies" in source
    assert "predecessor p_branch_insert posture drifted" in source
    assert "predecessor p_branch_select posture drifted" in source
    assert "predecessor p_branch_select already targets auth_runtime" in source


def test_auth_returning_policy_is_bound_to_real_db_identity_and_tenant() -> None:
    source = _source()
    returning = source.split("_RETURNING_POLICY_SQL =", 1)[1].split("def upgrade()", 1)[0]
    assert "current_user = session_user" in returning
    assert "pg_catalog.pg_has_role(session_user, 'auth_runtime', 'MEMBER')" in returning
    assert "auth.role() = 'owner'" in returning
    assert "app.current_org_id" in returning
    assert "app.current_user_id" in returning
    assert "app.current_principal_type" in returning
    assert "app.current_gym_id" in returning
    assert "('owner', 'legacy_gym_owner')" in returning
    assert "app_runtime" not in returning
    assert "TO PUBLIC" not in returning


def test_auth_returning_policy_only_exposes_canonical_initial_state() -> None:
    source = _source()
    returning = source.split("_RETURNING_POLICY_SQL =", 1)[1].split("def upgrade()", 1)[0]
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
        assert fragment in returning


def test_c87_downgrade_removes_only_the_p3a_owned_returning_policy() -> None:
    source = _source()
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY p_branch_select_auth_bootstrap_returning" in downgrade
    assert "ALTER POLICY" not in downgrade
    assert "DROP POLICY p_branch_insert" not in downgrade
    assert "DROP POLICY p_branch_select ON" not in downgrade
    assert "_require_returning_absent(bind)" in downgrade
    assert "_require_predecessor_policies(bind)" in downgrade
