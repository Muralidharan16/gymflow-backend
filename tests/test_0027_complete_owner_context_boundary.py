from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "alembic" / "versions" / "0027_rbac_p6_perm_snapshots.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=str(MIGRATION))


def _function(name: str) -> ast.FunctionDef:
    rows = [
        node
        for node in _tree().body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(rows) == 1
    return rows[0]


def _segment(name: str) -> str:
    source = _source()
    return ast.get_source_segment(source, _function(name)) or ""


def test_0027_uses_complete_owner_context_helpers():
    source = _source()
    for required in (
        "RB1M2S_0027_COMPLETE_OWNER_CONTEXT_AUTHORITY_HELPERS_START",
        "_rb1m2s_require_migration_owner",
        "_rb1m2s_can_set_security_owner",
        "_rb1m2s_run_as_security_owner",
        "_rb1m2s_prepare_owner_transfer",
        "_rb1m2s_restore_owner_transfer",
        "_rb1m2s_create_touch_trigger",
        "_rb1m2s_create_secure_view",
        "_rb1m2s_drop_secure_view",
        "_rb1m2s_drop_owned_functions",
    ):
        assert required in source


def test_0027_preflight_is_exact_and_fail_closed():
    text = _segment("_rb1m2s_preflight")
    for required in (
        "session_user",
        "current_user",
        "app_private",
        "app_secure",
        "migration_owner",
        "app_security_owner",
        "rolcanlogin",
        "rolinherit",
        "rolbypassrls",
        "pg_catalog.pg_has_role",
        "'SET'",
        "has_schema_privilege",
        "'USAGE'",
    ):
        assert required in _source() or required in text


def test_0027_touch_trigger_uses_temporary_table_trigger_authority():
    text = _segment("_rb1m2s_create_touch_trigger")
    ordered = (
        "GRANT TRIGGER ON TABLE",
        "_rb1m2s_run_as_security_owner",
        "CREATE TRIGGER trg_touch_perm_snapshot_updated_at",
        "REVOKE TRIGGER ON TABLE",
    )
    positions = [text.index(token) for token in ordered]
    assert positions == sorted(positions)
    for required in (
        "app_private.touch_updated_at()",
        "owner_name",
        "returns_trigger",
        "security_definer",
        "safe_search_path",
        "PUBLIC",
        "has_table_privilege",
    ):
        assert required in text


def test_0027_never_grants_touch_execute_to_migration_owner():
    source = _source()
    assert (
        "GRANT EXECUTE ON FUNCTION app_private.touch_updated_at() "
        "TO migration_owner"
        not in source
    )
    assert "ALTER FUNCTION app_private.touch_updated_at()" not in source


def test_0027_owner_transfer_create_acl_is_temporary_and_exact():
    prepare = _segment("_rb1m2s_prepare_owner_transfer")
    restore = _segment("_rb1m2s_restore_owner_transfer")
    assert "GRANT CREATE ON SCHEMA app_private" in prepare
    assert "has_schema_privilege" in prepare
    assert "REVOKE CREATE ON SCHEMA app_private" in restore
    assert "_rb1m2s_direct_private_create_acl" in restore
    assert 'state["before"]' in restore


def test_0027_mark_snapshot_function_sequence_is_safe():
    text = _segment("upgrade")
    create = text.index(
        "CREATE OR REPLACE FUNCTION app_private.mark_snapshot_stale()"
    )
    revoke = text.index(
        "REVOKE ALL ON FUNCTION app_private.mark_snapshot_stale() "
        "FROM PUBLIC"
    )
    comment = text.index(
        "COMMENT ON FUNCTION app_private.mark_snapshot_stale()"
    )
    trigger = text.index("CREATE TRIGGER trg_invalidate_perm_snapshot")
    transfer = text.index(
        "ALTER FUNCTION app_private.mark_snapshot_stale() "
        "OWNER TO app_security_owner"
    )
    assert create < revoke < comment < trigger < transfer


def test_0027_compile_function_sequence_is_safe():
    text = _segment("upgrade")
    create = text.index(
        "CREATE OR REPLACE FUNCTION "
        "app_private.compile_member_permissions("
    )
    revoke = text.index(
        "REVOKE ALL ON FUNCTION "
        "app_private.compile_member_permissions"
    )
    grant = text.index(
        "GRANT EXECUTE ON FUNCTION "
        "app_private.compile_member_permissions"
    )
    comment = text.index(
        "COMMENT ON FUNCTION "
        "app_private.compile_member_permissions"
    )
    transfer = text.index(
        "ALTER FUNCTION "
        "app_private.compile_member_permissions"
    )
    assert create < revoke < grant < comment < transfer


def test_0027_both_function_transfers_share_one_acl_window():
    text = _segment("upgrade")
    prepare = text.index("_rb1m2s_prepare_owner_transfer(bind)")
    mark_transfer = text.index(
        "ALTER FUNCTION app_private.mark_snapshot_stale() "
        "OWNER TO app_security_owner"
    )
    compile_transfer = text.index(
        "ALTER FUNCTION "
        "app_private.compile_member_permissions"
    )
    restore = text.index("_rb1m2s_restore_owner_transfer(bind, owner_state)")
    assert prepare < mark_transfer < compile_transfer < restore
    assert text.count("_rb1m2s_prepare_owner_transfer(bind)") == 1
    assert text.count("_rb1m2s_restore_owner_transfer(") == 1


def test_0027_view_creation_is_bounded_and_security_invoker():
    text = _segment("_rb1m2s_create_secure_view")
    for required in (
        "GRANT SELECT ON TABLE",
        "public.member_permission_snapshots",
        "_rb1m2s_run_as_security_owner",
        "CREATE OR REPLACE VIEW",
        "security_barrier = true",
        "security_invoker = true",
        "REVOKE ALL ON TABLE",
        "FROM PUBLIC",
        "TO app_runtime, readonly_analytics",
        "COMMENT ON VIEW",
        "REVOKE SELECT ON TABLE",
        "FROM app_security_owner",
    ):
        assert required in text


def test_0027_view_preflight_preserves_predecessor_scope_authority():
    text = _segment("_rb1m2s_create_secure_view")
    assert "predecessor scope_types SELECT" in text
    assert "snapshot-table SELECT authority" in text
    assert (
        "REVOKE SELECT ON TABLE public.scope_types "
        "FROM app_security_owner"
        not in _source()
    )


def test_0027_view_downgrade_runs_as_owner_with_restrict():
    text = _segment("_rb1m2s_drop_secure_view")
    assert "_rb1m2s_run_as_security_owner" in text
    assert "DROP VIEW" in text
    assert "RESTRICT" in text
    assert "CASCADE" not in text


def test_0027_function_downgrade_runs_as_owner_with_restrict():
    text = _segment("_rb1m2s_drop_owned_functions")
    assert "_rb1m2s_run_as_security_owner" in text
    assert "DROP FUNCTION" in text
    assert "compile_member_permissions" in text
    assert "mark_snapshot_stale" in text
    assert text.count("RESTRICT") == 2
    assert "CASCADE" not in text


def test_0027_downgrade_orders_view_triggers_functions_and_table():
    text = _segment("downgrade")
    ordered = (
        "_rb1m2s_drop_secure_view(bind)",
        "DROP TRIGGER IF EXISTS trg_invalidate_perm_snapshot",
        "DROP TRIGGER IF EXISTS trg_touch_perm_snapshot_updated_at",
        "_rb1m2s_drop_owned_functions(bind)",
        "DROP POLICY IF EXISTS tenant_isolation_permission_snapshots",
        "DROP TABLE IF EXISTS public.member_permission_snapshots",
    )
    positions = [text.index(token) for token in ordered]
    assert positions == sorted(positions)


def test_0027_has_no_broad_or_persistent_authority_workaround():
    source = _source()
    for forbidden in (
        "GRANT CREATE ON SCHEMA app_secure",
        "ALTER SCHEMA app_secure OWNER TO",
        "OWNER TO migration_owner",
        "OWNER TO postgres",
        "GRANT EXECUTE ON FUNCTION app_private.touch_updated_at",
        "GRANT ALL ON FUNCTION",
        "DROP VIEW IF EXISTS app_secure.v_effective_member_permissions;",
        "DROP FUNCTION IF EXISTS app_private.compile_member_permissions",
        "DROP FUNCTION IF EXISTS app_private.mark_snapshot_stale",
    ):
        assert forbidden not in source


def test_0027_unrelated_permission_snapshot_contract_remains():
    source = _source()
    for required in (
        "CREATE TABLE public.member_permission_snapshots",
        "CREATE INDEX ix_perm_snap_member_branch_fresh",
        "CREATE INDEX ix_perm_snap_org_stale",
        "CREATE INDEX ix_perm_snap_member_version",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "tenant_isolation_permission_snapshots",
        "GRANT SELECT, INSERT, UPDATE ON public.member_permission_snapshots",
    ):
        assert required in source

def test_0027_never_resolves_public_as_a_role_name():
    source = _source()
    for forbidden in (
        "has_function_privilege(\n                'PUBLIC'",
        "has_table_privilege(\n                'PUBLIC'",
        'has_function_privilege(\n                "PUBLIC"',
        'has_table_privilege(\n                "PUBLIC"',
    ):
        assert forbidden not in source


def test_0027_public_function_execute_uses_effective_acl_grantee_zero():
    text = _segment("_rb1m2t_public_has_function_execute")
    for required in (
        "pg_catalog.aclexplode",
        "COALESCE",
        "function_data.proacl",
        "pg_catalog.acldefault",
        "'f'::\"char\"",
        "function_data.proowner",
        "acl.grantee = 0",
        "acl.privilege_type = 'EXECUTE'",
        "pg_catalog.to_regprocedure(:signature)",
    ):
        assert required in text


def test_0027_public_relation_select_uses_effective_acl_grantee_zero():
    text = _segment("_rb1m2u_public_has_relation_select")
    for required in (
        "pg_catalog.pg_class AS relation",
        "JOIN pg_catalog.pg_namespace AS namespace",
        "namespace.oid = relation.relnamespace",
        "pg_catalog.aclexplode",
        "COALESCE",
        "relation.relacl",
        "pg_catalog.acldefault",
        "WHEN relation.relkind = 'S'",
        "THEN 's'::\"char\"",
        "ELSE 'r'::\"char\"",
        "relation.relowner",
        "namespace.nspname = :schema_name",
        "relation.relname = :relation_name",
        "acl.grantee = 0",
        "acl.privilege_type = 'SELECT'",
    ):
        assert required in text
    assert "pg_catalog.to_regclass" not in text


def test_0027_public_acl_helpers_are_used_by_runtime_preflights():
    source = _source()
    touch = _segment("_rb1m2s_create_touch_trigger")
    view = _segment("_rb1m2s_create_secure_view")
    assert "_rb1m2t_public_has_function_execute(" in touch
    assert "_RB1M2S_TOUCH_FUNCTION" in touch
    expected_public_acl_call = """if _rb1m2u_public_has_relation_select(
        bind,
        _RB1M2S_SECURE_SCHEMA,
        "v_effective_member_permissions",
    ):"""
    assert expected_public_acl_call in view
    assert source.count("_rb1m2t_public_has_function_execute(") == 2
    assert source.count("_rb1m2u_public_has_relation_select(") == 2

def test_0027_protected_relation_acl_check_avoids_regclass_resolution():
    text = _segment("_rb1m2u_public_has_relation_select")
    assert "pg_catalog.to_regclass" not in text
    assert "_RB1M2S_VIEW" not in text


def test_0027_protected_relation_acl_check_uses_catalog_namespace_join():
    text = _segment("_rb1m2u_public_has_relation_select")
    for required in (
        "pg_catalog.pg_class AS relation",
        "JOIN pg_catalog.pg_namespace AS namespace",
        "namespace.oid = relation.relnamespace",
        "namespace.nspname = :schema_name",
        "relation.relname = :relation_name",
        "relation.relkind IN",
        "acl.grantee = 0",
        "acl.privilege_type = 'SELECT'",
    ):
        assert required in text


def test_0027_protected_view_acl_check_passes_unqualified_catalog_identity():
    text = _segment("_rb1m2s_create_secure_view")
    expected_public_acl_call = """if _rb1m2u_public_has_relation_select(
        bind,
        _RB1M2S_SECURE_SCHEMA,
        "v_effective_member_permissions",
    ):"""
    assert expected_public_acl_call in text


def test_0027_does_not_expand_migration_owner_app_secure_authority():
    source = _source()
    for forbidden in (
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner",
        "GRANT CREATE ON SCHEMA app_secure TO migration_owner",
        "ALTER SCHEMA app_secure OWNER TO migration_owner",
    ):
        assert forbidden not in source

def test_0027_reader_grant_verification_avoids_qualified_name_privilege_api():
    text = _segment("_rb1m2s_create_secure_view")
    forbidden = """SELECT pg_catalog.has_table_privilege(
                    :role_name,
                    :view_name,
                    'SELECT'
                )"""
    assert forbidden not in text


def test_0027_reader_grant_helper_uses_direct_catalog_acl_identity():
    text = _segment("_rb1m2v_role_has_direct_relation_select")
    for required in (
        "pg_catalog.pg_class AS relation",
        "JOIN pg_catalog.pg_namespace AS namespace",
        "JOIN pg_catalog.pg_roles AS grantee_role",
        "JOIN pg_catalog.pg_roles AS grantor_role",
        "pg_catalog.aclexplode",
        "COALESCE",
        "pg_catalog.acldefault",
        "namespace.nspname = :schema_name",
        "relation.relname = :relation_name",
        "acl.grantee = grantee_role.oid",
        "acl.grantor = grantor_role.oid",
        "acl.privilege_type = 'SELECT'",
        "acl.is_grantable IS FALSE",
    ):
        assert required in text
    assert "pg_catalog.to_regclass" not in text
    assert "pg_catalog.has_table_privilege" not in text


def test_0027_reader_grant_verification_requires_exact_direct_grants():
    text = _segment("_rb1m2s_create_secure_view")
    expected_call = """if not _rb1m2v_role_has_direct_relation_select(
            bind,
            _RB1M2S_SECURE_SCHEMA,
            "v_effective_member_permissions",
            role_name,
            _RB1M2S_SECURITY_OWNER,
        ):"""
    assert expected_call in text
    assert 'for role_name in ("app_runtime", "readonly_analytics")' in text
    assert "protected-view direct reader grant" in text


def test_0027_reader_acl_fix_does_not_expand_protected_schema_authority():
    source = _source()
    for forbidden in (
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner",
        "GRANT CREATE ON SCHEMA app_secure TO migration_owner",
        "ALTER SCHEMA app_secure OWNER TO migration_owner",
        "GRANT USAGE ON SCHEMA app_secure TO app_runtime",
        "GRANT USAGE ON SCHEMA app_secure TO readonly_analytics",
    ):
        assert forbidden not in source

