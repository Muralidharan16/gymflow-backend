from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M24 = ROOT / "alembic" / "versions" / "0024_rbac_p3_org_members.py"
M25 = ROOT / "alembic" / "versions" / "0025_rbac_p4_bsr_expand.py"
M29 = ROOT / "alembic" / "versions" / "0029_rbac_p8_contract.py"


def src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_0024_seeds_every_preexisting_organization_user():
    text = src(M24)
    assert "RB1M2U_0024_EXISTING_USER_SEED_START" in text
    assert "INSERT INTO public.organization_members" in text
    assert "FROM public.organization_users AS ou" in text
    assert "WHEN ou.deleted_at IS NOT NULL THEN 5" in text
    assert "WHEN ou.is_active = TRUE THEN 3" in text
    assert "ELSE 4" in text


def test_0024_seed_uses_bounded_force_rls_window_and_restores_it():
    text = src(M24)
    assert "ALTER TABLE public.organization_users " in text
    assert "NO FORCE ROW LEVEL SECURITY;" in text
    assert "FORCE ROW LEVEL SECURITY;" in text
    assert "DISABLE ROW LEVEL SECURITY" not in text
    assert "session_user <> 'migration_owner'" in text
    assert "current_user <> 'migration_owner'" in text


def test_0024_seed_is_verified_for_state_and_cardinality():
    text = src(M24)
    assert "0024 organization_members seed verification failed" in text
    assert "0024 organization_members seed cardinality mismatch" in text
    assert "om.membership_status_id IS DISTINCT FROM" in text
    assert "om.deleted_at IS DISTINCT FROM ou.deleted_at" in text


def test_0025_backfills_both_new_representations():
    text = src(M25)
    assert "RB1M2U_0025_EXPAND_BACKFILL_START" in text
    assert "SET organization_member_id = om.id" in text
    assert "SET role_id = sr.id" in text
    assert "0025 expand backfill left unresolved or inconsistent rows" in text


def test_0025_backfill_uses_bounded_cross_tenant_owner_window():
    text = src(M25)
    assert text.count("NO FORCE ROW LEVEL SECURITY;") >= 2
    assert text.count("FORCE ROW LEVEL SECURITY;") >= 2
    assert "DISABLE ROW LEVEL SECURITY" not in text
    assert "LOCK TABLE public.organization_members" in text
    assert "LOCK TABLE public.branch_staff_roles" in text


def test_0025_installs_bidirectional_dual_write_sync():
    text = src(M25)
    assert "sync_branch_staff_role_contract_fields()" in text
    assert "SECURITY INVOKER" in text
    assert "CREATE TRIGGER trg_sync_branch_staff_role_contract_fields" in text
    assert "BEFORE INSERT OR UPDATE" in text
    assert "NEW.user_id := v_user_id" in text
    assert "NEW.role := v_role_code::public.branch_staff_role_enum" in text
    assert "branch_staff_roles user/member identity mismatch" in text
    assert "branch_staff_roles legacy/canonical role mismatch" in text


def test_0025_downgrade_removes_expand_synchronizer_through_bounded_owner_context():
    text = src(M25)
    down = text[text.index("def downgrade() -> None:") :]
    helper = text[
        text.index("def _rb1m2u_assert_sync_contract") :
        text.index("def upgrade() -> None:")
    ]
    assert "RB1M2U_0025_EXPAND_DOWNGRADE_CLEANUP" in down
    assert 'row["owner_name"] != "migration_owner"' in helper
    assert 'row["security_definer"] is not False' in helper
    assert "PUBLIC EXECUTE on the 0025 synchronizer is forbidden" in helper
    assert "trigger_matches_function" in helper
    assert (
        down.index("_rb1m2u_assert_sync_contract(bind)")
        < down.index("_rb1m2a_drop_secure_view(bind)")
    )
    assert "DROP TRIGGER trg_sync_branch_staff_role_contract_fields" in down
    assert '_rb1m2a_run_as_role(\n        bind,\n        "migration_owner"' in down
    assert (
        "DROP FUNCTION "
        "app_private.sync_branch_staff_role_contract_fields() RESTRICT"
        in down
    )
    assert "DROP FUNCTION IF EXISTS" not in down
    assert (
        down.index("DROP TRIGGER trg_sync_branch_staff_role_contract_fields")
        < down.index("DROP POLICY IF EXISTS tenant_isolation_staff_roles")
    )


def test_0029_journals_exact_predecessor_contracts():
    text = src(M29)
    assert "migration_0029_contract_state" in text
    assert "pg_catalog.pg_get_functiondef" in text
    assert "pg_catalog.pg_get_viewdef" in text
    assert "pg_catalog.pg_get_constraintdef" in text
    assert "pg_catalog.pg_get_indexdef" in text
    for token in (
        "app_private.mark_snapshot_stale()",
        "app_private.compile_member_permissions(uuid,uuid,uuid,smallint)",
        "app_private.handle_user_deactivation_cascade()",
        "app_private.log_branch_staff_role_audit()",
        "app_private.sync_branch_staff_role_contract_fields()",
    ):
        assert token in text


def test_0029_journals_expand_constraint_validation_state():
    text = src(M29)
    assert "_EXPAND_CONSTRAINTS" in text
    assert "con.convalidated AS validated" in text
    assert "_restore_expand_constraint_validation(bind)" in text
    assert "NOT VALID" in text


def test_0029_drops_legacy_actor_fks_before_actor_conversion():
    text = src(M29)
    upgrade = text[text.index("def upgrade() -> None:") : text.index("def downgrade() -> None:")]
    drop = upgrade.index("DROP CONSTRAINT fk_branch_staff_assigned_by")
    update = upgrade.index("SET assigned_by = om.id")
    assert drop < update


def test_0029_actor_fks_are_same_tenant_composites():
    text = src(M29)
    upgrade = text[text.index("def upgrade() -> None:") : text.index("def downgrade() -> None:")]
    assert "FOREIGN KEY (assigned_by, org_id)" in upgrade
    assert "FOREIGN KEY (revoked_by, org_id)" in upgrade
    assert "REFERENCES public.organization_members(id, org_id)" in upgrade
    assert "VALIDATE CONSTRAINT fk_bsr_assigned_by" in upgrade
    assert "VALIDATE CONSTRAINT fk_bsr_revoked_by" in upgrade


def test_0029_validates_replacements_before_dropping_source_columns():
    text = src(M29)
    upgrade = text[text.index("def upgrade() -> None:") : text.index("def downgrade() -> None:")]
    validation = upgrade.index("VALIDATE CONSTRAINT fk_bsr_revoked_by")
    user_drop = upgrade.index("DROP COLUMN user_id")
    assert validation < user_drop
    assert "ALTER COLUMN organization_member_id SET NOT NULL" in upgrade
    assert "ALTER COLUMN role_id SET NOT NULL" in upgrade


def test_0029_maintenance_never_disables_rls_or_uses_global_bypass():
    text = src(M29)
    assert "NO FORCE ROW LEVEL SECURITY" in text
    assert "DISABLE ROW LEVEL SECURITY" not in text
    assert "BYPASSRLS" not in text
    assert "session_replication_role" not in text
    assert "DISABLE TRIGGER trg_bsr_validate_rls_context" in text
    assert "DISABLE TRIGGER trg_invalidate_perm_snapshot" in text
    assert "ENABLE TRIGGER trg_bsr_validate_rls_context" in text
    assert "ENABLE TRIGGER trg_invalidate_perm_snapshot" in text


def test_0029_runtime_permission_functions_are_tenant_bound_with_rls_on():
    text = src(M29)
    assert "CREATE OR REPLACE FUNCTION app_private.mark_snapshot_stale()" in text
    assert "CREATE OR REPLACE FUNCTION app_private.compile_member_permissions(" in text
    assert text.count("SET row_security = on") >= 2
    assert "Snapshot invalidation tenant mismatch" in text
    assert "Permission compilation tenant mismatch" in text
    assert "rbac_internal_staff_roles_select" in text


def test_0029_runtime_deactivation_and_audit_translate_member_actors():
    text = src(M29)
    assert "CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()" in text
    assert "revoked_by = v_actor_member" in text
    assert "CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()" in text
    assert "assigned_by_member_id" in text
    assert "revoked_by_member_id" in text
    assert "v_fallback_user" in text


def test_0029_secure_view_stays_invoker_and_never_uses_cascade():
    text = src(M29)
    assert "security_barrier = true" in text
    assert "security_invoker = true" in text
    assert "DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT" in text
    assert "CASCADE" not in text
    assert "om.user_id AS user_id" in text
    assert "sr.code AS role_legacy" in text


def test_0029_privilege_deltas_are_allowlisted_and_reversible():
    text = src(M29)
    assert "migration_0029_added_grants" in text
    assert "_ADDED_GRANT_ALLOWLIST" in text
    assert "_grant_if_needed" in text
    assert "_revoke_revision_added_grants" in text
    assert "Unapproved grant journal row" in text
    assert "PUBLIC CREATE on app_private is forbidden" in text


def test_0029_downgrade_reconstructs_legacy_data_and_fails_closed():
    text = src(M29)
    down = text[text.index("def downgrade() -> None:") :]
    assert "ADD COLUMN user_id UUID NULL" in down
    assert "ADD COLUMN role public.branch_staff_role_enum NULL" in down
    assert "SET user_id = om.user_id" in down
    assert "SET role = sr.code::public.branch_staff_role_enum" in down
    assert "ALTER COLUMN user_id SET NOT NULL" in down
    assert "ALTER COLUMN role SET NOT NULL" in down
    assert "cannot represent canonical owner/admin" in down
    assert "_restore_legacy_objects(bind)" in down
    assert "_restore_functions(bind)" in down
    assert "_restore_predecessor_view(bind)" in down


def test_0029_restores_force_rls_triggers_and_temporary_create_authority():
    text = src(M29)
    assert text.count("_require_forced_owner_tables(bind)") >= 4
    assert text.count('_require_trigger_states(bind, "O")') >= 4
    assert "_prepare_private_create" in text
    assert "_release_private_create" in text
    assert "Bounded CREATE on app_private leaked" in text


def test_0025_sync_acl_assertion_models_public_as_acl_pseudorole_not_database_role():
    text = src(M25)
    helper = text[
        text.index("def _rb1m2u_assert_sync_contract") :
        text.index("# RB1M2U_0025_SYNC_CONTRACT_HELPER_END")
    ]
    assert "has_function_privilege(" not in helper
    assert "pg_catalog.aclexplode" in helper
    assert "pg_catalog.acldefault" in helper
    assert "acl_data.grantee = 0" in helper
    assert "THEN 'PUBLIC'" in helper
    assert "allowed_execute_grantees" in helper
    assert '"migration_owner"' in helper
    assert '"app_runtime"' in helper
    assert '"app_rls_executor"' in helper
    assert "unexpected direct EXECUTE ACL grantees" in helper
    assert "unexpected grantor" in helper
    assert "carry grant option" in helper


def test_0029_public_schema_acl_assertion_models_public_as_acl_pseudorole_not_database_role():
    text = src(M29)
    helper = text[
        text.index("def _public_schema_privilege") :
        text.index("def _reject_public_private_create")
    ]
    reject = text[
        text.index("def _reject_public_private_create") :
        text.index("def _require_role_foundation")
    ]
    assert "pg_catalog.aclexplode" in helper
    assert "pg_catalog.acldefault" in helper
    assert "acl_data.grantee = 0" in helper
    assert "namespace_data.nspacl" in helper
    assert "namespace_data.nspowner" in helper
    assert "privilege.upper()" in helper
    assert "schema_count" in helper
    assert "public_has_privilege" in helper
    assert '_schema_privilege(bind, "PUBLIC"' not in text
    assert '_public_schema_privilege(bind, "app_private", "CREATE")' in reject
    assert "PUBLIC CREATE on app_private is forbidden" in reject


def test_0029_app_secure_catalog_inspection_does_not_require_migration_owner_schema_usage():
    text = src(M29)
    foundation = text[
        text.index("def _require_role_foundation") :
        text.index("def _prepare_private_create")
    ]
    capture = text[
        text.index("def _capture_view_state") :
        text.index("def _capture_constraint_state")
    ]

    assert "'app_secure.v_active_branch_staff_roles'::regclass" not in text
    assert "JOIN pg_catalog.pg_namespace AS n" in foundation
    assert "n.nspname = 'app_secure'" in foundation
    assert "c.relname = 'v_active_branch_staff_roles'" in foundation

    assert "c.oid::oid AS relation_oid" in capture
    assert "JOIN pg_catalog.pg_namespace AS n" in capture
    assert "pg_catalog.aclexplode" in capture
    assert "pg_catalog.acldefault" in capture
    assert "'r'::\"char\"" in capture
    assert "acl_data.grantee = 0" in capture
    assert "acl_data.privilege_type = 'SELECT'" in capture
    assert 'if row["public_select"]:' in capture
    assert "'PUBLIC', 'app_secure.v_active_branch_staff_roles'" not in capture
    assert ":role_name, 'app_secure.v_active_branch_staff_roles'" not in capture
    assert ":role_name, CAST(:relation_oid AS oid), 'SELECT'" in capture

    assert '"DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT"' in text
    assert "CREATE VIEW app_secure.v_active_branch_staff_roles" in text
    assert '"REVOKE ALL ON app_secure.v_active_branch_staff_roles FROM PUBLIC"' in text


def test_0029_predecessor_journal_binds_have_explicit_types_for_asyncpg_polymorphic_json():
    text = src(M29)
    capture_view = text[
        text.index("def _capture_view_state") :
        text.index("def _capture_constraint_state")
    ]
    capture_constraints = text[
        text.index("def _capture_constraint_state") :
        text.index("def _capture_index_state")
    ]

    assert "'comment', CAST(:comment_text AS text)" in capture_view
    assert "'comment', :comment_text" not in capture_view

    assert "CAST(:validated AS boolean)" in capture_constraints
    assert "jsonb_build_object('validated', :validated)" not in capture_constraints

    # Keep the already-explicit JSON cast for relation options.
    assert "'reloptions', CAST(:reloptions AS jsonb)" in capture_view


def test_0029_downgrade_suspends_audit_trigger_for_representation_only_reconstruction():
    text = src(M29)
    downgrade = text[text.index("def downgrade() -> None:") :]

    assert "def _require_audit_trigger_state" in text
    assert '_require_audit_trigger_state(bind, "O")' in downgrade
    assert (
        "DISABLE TRIGGER trg_audit_branch_staff_roles;"
        in downgrade
    )
    assert '_require_audit_trigger_state(bind, "D")' in downgrade
    assert (
        "ENABLE TRIGGER trg_audit_branch_staff_roles;"
        in downgrade
    )

    disable_pos = downgrade.index(
        "DISABLE TRIGGER trg_audit_branch_staff_roles;"
    )
    for rewrite in (
        "SET user_id = om.user_id",
        "SET role = sr.code::public.branch_staff_role_enum",
        "SET assigned_by = om.user_id",
        "SET revoked_by = om.user_id",
    ):
        assert disable_pos < downgrade.index(rewrite)

    restore_functions_pos = downgrade.index("_restore_functions(bind)")
    enable_pos = downgrade.index(
        "ENABLE TRIGGER trg_audit_branch_staff_roles;"
    )
    assert restore_functions_pos < enable_pos

    # Existing narrow maintenance suppressions remain intact.
    assert "DISABLE TRIGGER trg_bsr_validate_rls_context;" in downgrade
    assert "DISABLE TRIGGER trg_invalidate_perm_snapshot;" in downgrade
    assert "DISABLE TRIGGER ALL" not in downgrade
    assert "session_replication_role" not in downgrade
    assert "SET LOCAL app.current_org_id" not in downgrade


def test_0029_downgrade_opens_bounded_organization_users_owner_window_for_legacy_fk_validation():
    text = src(M29)
    downgrade = text[text.index("def downgrade() -> None:") :]

    assert "def _require_forced_owner_organization_users" in text
    assert '_require_forced_owner_organization_users(bind)' in downgrade

    lock = (
        '"LOCK TABLE public.organization_users "'
        '\n        "IN SHARE ROW EXCLUSIVE MODE;"'
    )
    assert lock in downgrade
    assert "LOCK TABLE public.organization_users IN SHARE MODE;" not in downgrade

    no_force = (
        '"ALTER TABLE public.organization_users "'
        '\n        "NO FORCE ROW LEVEL SECURITY;"'
    )
    force = (
        '"ALTER TABLE public.organization_users "'
        '\n        "FORCE ROW LEVEL SECURITY;"'
    )

    no_force_pos = downgrade.index(no_force)
    restore_legacy_pos = downgrade.index("_restore_legacy_objects(bind)")
    force_pos = downgrade.index(force, restore_legacy_pos)
    restore_expand_pos = downgrade.index(
        "_restore_expand_constraint_validation(bind)"
    )
    assert no_force_pos < restore_legacy_pos < force_pos < restore_expand_pos

    for name in (
        '"fk_branch_staff_assigned_by"',
        '"fk_branch_staff_revoked_by"',
        '"fk_branch_staff_user_org"',
    ):
        assert name in text

    assert "SET assigned_by = om.user_id" in downgrade
    assert "SET revoked_by = om.user_id" in downgrade
    assert "DISABLE TRIGGER trg_bsr_validate_rls_context;" in downgrade
    assert "DISABLE TRIGGER trg_invalidate_perm_snapshot;" in downgrade
    assert "DISABLE TRIGGER trg_audit_branch_staff_roles;" in downgrade

    assert "DISABLE ROW LEVEL SECURITY" not in downgrade
    assert "SET LOCAL app.current_org_id" not in downgrade
    assert "DISABLE TRIGGER ALL" not in downgrade
    assert "session_replication_role" not in downgrade


def test_0029_app_runtime_compile_authority_includes_reversible_private_schema_usage():
    text = src(M29)
    upgrade = text[
        text.index("def upgrade() -> None:") :
        text.index("def downgrade() -> None:")
    ]
    downgrade = text[text.index("def downgrade() -> None:") :]

    assert '("schema", "app_private", "app_runtime", "USAGE")' in text

    grant_helper = text[
        text.index("def _grant_if_needed") :
        text.index("def _prepare_runtime_authority")
    ]
    assert 'elif object_kind == "schema":' in grant_helper
    assert "_schema_privilege(" in grant_helper
    assert "GRANT {privilege} ON SCHEMA {object_identity} TO {grantee}" in grant_helper

    authority = text[
        text.index("def _require_app_runtime_compile_authority") :
        text.index("def _replace_runtime_functions")
    ]
    assert '"app_runtime", "app_private", "USAGE"' in authority
    assert '"app_runtime", "app_private", "CREATE"' in authority
    assert "app_runtime must not have CREATE on app_private." in authority
    assert '_public_schema_privilege(bind, "app_private", "USAGE")' in authority
    assert "PUBLIC USAGE on app_private is forbidden." in authority
    assert "pg_catalog.has_function_privilege(" in authority
    assert "app_private.compile_member_permissions" in authority

    revoke = text[
        text.index("def _revoke_revision_added_grants") :
        text.index("def _check_policy_collisions")
    ]
    assert 'elif row["object_kind"] == "schema":' in revoke
    assert "REVOKE {row['privilege_type']} ON SCHEMA " in revoke

    assert (
        upgrade.index("_prepare_runtime_authority(bind)")
        < upgrade.index("_replace_runtime_functions(bind)")
        < upgrade.index("_require_app_runtime_compile_authority(bind)")
    )
    assert (
        downgrade.index("_require_app_runtime_compile_authority(bind)")
        < downgrade.index("_revoke_revision_added_grants(bind)")
    )

    assert "GRANT CREATE ON SCHEMA app_private TO app_runtime" not in text
    assert "GRANT USAGE ON SCHEMA app_private TO PUBLIC" not in text
    assert "GRANT EXECUTE ON ALL FUNCTIONS" not in text
