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


def test_c87_adds_a_separate_auth_only_bootstrap_policy() -> None:
    source = _source()
    assert '_LIFECYCLE_POLICY = "p_branch_insert"' in source
    assert '_BOOTSTRAP_POLICY = "p_branch_insert_auth_bootstrap"' in source
    assert "CREATE POLICY p_branch_insert_auth_bootstrap" in source
    assert "ON public.org_branch_state" in source
    assert "AS PERMISSIVE" in source
    assert "FOR INSERT" in source
    assert "TO auth_runtime" in source
    assert "ALTER POLICY p_branch_insert " not in source
    assert "auth bootstrap policy must target exactly auth_runtime" in source
    assert "policy_data.polroles = ARRAY[" in source


def test_c87_proves_lifecycle_policy_is_unchanged_in_both_directions() -> None:
    source = _source()
    assert "lifecycle_before = _require_lifecycle_policy(bind)" in source
    assert "lifecycle_after = _require_lifecycle_policy(bind)" in source
    assert source.count("if lifecycle_after != lifecycle_before:") == 2
    assert "c87 changed the predecessor lifecycle policy" in source
    assert "c87 downgrade changed the predecessor lifecycle policy" in source
    assert "p_branch_insert lost lifecycle token" in source


def test_auth_bootstrap_policy_is_bound_to_real_db_identity_and_tenant() -> None:
    source = _source()
    bootstrap = source.split("_BOOTSTRAP_SQL =", 1)[1].split("def upgrade()", 1)[0]
    assert "current_user = session_user" in bootstrap
    assert "pg_catalog.pg_has_role(session_user, 'auth_runtime', 'MEMBER')" in bootstrap
    assert "auth.role() = 'owner'" in bootstrap
    assert "app.current_org_id" in bootstrap
    assert "app.current_user_id" in bootstrap
    assert "app.current_principal_type" in bootstrap
    assert "app.current_gym_id" in bootstrap
    assert "('owner', 'legacy_gym_owner')" in bootstrap
    assert "app_runtime" not in bootstrap
    assert "TO PUBLIC" not in bootstrap


def test_auth_bootstrap_policy_only_accepts_canonical_initial_state() -> None:
    source = _source()
    bootstrap = source.split("_BOOTSTRAP_SQL =", 1)[1].split("def upgrade()", 1)[0]
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
        assert fragment in bootstrap


def test_c87_downgrade_removes_only_the_p3a_owned_bootstrap_policy() -> None:
    source = _source()
    downgrade = source.split("def downgrade()", 1)[1]
    assert (
        'DROP POLICY p_branch_insert_auth_bootstrap ON public.org_branch_state'
        in downgrade
    )
    assert "ALTER POLICY" not in downgrade
    assert "DROP POLICY p_branch_insert ON" not in downgrade
    assert "_require_bootstrap_absent(bind)" in downgrade
    assert "_require_lifecycle_policy(bind)" in downgrade
