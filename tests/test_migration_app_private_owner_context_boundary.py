from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0021_staff_roles.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=str(MIGRATION))


def _function(name: str) -> ast.FunctionDef:
    for node in _tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Missing function: {name}")


def _function_source(name: str) -> str:
    source = _source()
    node = _function(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _string_constants(node: ast.AST) -> list[str]:
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _helper_source() -> str:
    source = _source()
    start = source.index(
        "# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_START"
    )
    end = source.index(
        "# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_END"
    )
    return source[start:end]


def _upgrade_source() -> str:
    return _function_source("upgrade")


def _all_positions(text: str, token: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        index = text.find(token, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + len(token)


def test_app_private_owner_context_helpers_are_frozen_and_migration_local():
    source = _source()
    helper = _helper_source()
    preflight_node = _function(
        "_rb1l8d1a_preflight_upgrade_owner_context"
    )
    preflight = _function_source(
        "_rb1l8d1a_preflight_upgrade_owner_context"
    )
    prepare_node = _function(
        "_rb1l8d1a_prepare_upgrade_owner_context"
    )
    prepare = _function_source(
        "_rb1l8d1a_prepare_upgrade_owner_context"
    )
    upgrade_node = _function("upgrade")
    upgrade = _upgrade_source()
    assert source.count(
        "# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_START"
    ) == 1
    assert source.count(
        "# RB1L8D1A_APP_PRIVATE_OWNER_CONTEXT_HELPERS_END"
    ) == 1
    assert "from alembic.versions" not in source
    assert "importlib" not in helper
    assert "migration_0021" not in helper
    assert "CREATE TABLE" not in helper
    assert "persistent marker" not in helper.lower()
    assert "Frozen revision-local contract" in helper
    function_names = [
        node.name
        for node in _tree().body
        if isinstance(node, ast.FunctionDef)
    ]
    assert function_names.count(
        "_rb1l8d1a_preflight_upgrade_owner_context"
    ) == 1

    preflight_calls = [
        item
        for item in ast.walk(preflight_node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
    ]
    preflight_call_names = [
        item.func.id
        for item in sorted(
            preflight_calls,
            key=lambda item: (item.lineno, item.col_offset),
        )
    ]
    assert preflight_call_names == [
        "_rb1l8d1a_require_migration_owner",
        "_rb1l8d1a_require_set_capability",
        "_rb1l8d1a_require_migration_owner_schema_capabilities",
        "_rb1l8d1a_reject_public_create",
    ]
    assert "_rb1l8d1a_require_target_owner_capabilities" not in preflight
    mutation_tokens = (
        "GRANT ",
        "REVOKE ",
        "CREATE ",
        "ALTER ",
        "DROP ",
        "SET ROLE",
        "SET LOCAL ROLE",
        "RESET ROLE",
    )
    for token in mutation_tokens:
        assert token not in preflight
    for call in ast.walk(preflight_node):
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        assert call.func.attr != "execute"

    prepare_body = prepare_node.body
    assert prepare_body
    first_prepare = prepare_body[0]
    assert isinstance(first_prepare, ast.Expr)
    assert isinstance(first_prepare.value, ast.Call)
    assert isinstance(first_prepare.value.func, ast.Name)
    assert (
        first_prepare.value.func.id
        == "_rb1l8d1a_preflight_upgrade_owner_context"
    )
    assert prepare.count(
        "_rb1l8d1a_preflight_upgrade_owner_context(bind)"
    ) == 1
    assert "_rb1l8d1a_require_target_owner_capabilities" not in prepare

    direct_preflight_calls = [
        item
        for item in ast.walk(upgrade_node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id
        == "_rb1l8d1a_preflight_upgrade_owner_context"
    ]
    assert len(direct_preflight_calls) == 1
    assert direct_preflight_calls[0].lineno == upgrade_node.body[1].lineno
    assert isinstance(upgrade_node.body[0], ast.Assign)
    assert isinstance(upgrade_node.body[1], ast.Expr)
    assert upgrade.index("preflight_bind = _rb1l8d1a_bind()") < upgrade.index(
        "_rb1l8d1a_preflight_upgrade_owner_context(preflight_bind)"
    )
    preflight_position = upgrade.index(
        "_rb1l8d1a_preflight_upgrade_owner_context(preflight_bind)"
    )
    assert preflight_position < upgrade.index("op.execute(")
    prefix = upgrade[:preflight_position]
    assert "op.execute(" not in prefix
    assert "autocommit_block" not in source
    assert "CONCURRENTLY" not in source.upper()

    prepare_position = upgrade.index(
        "owner_state = _rb1l8d1a_prepare_upgrade_owner_context(bind)"
    )
    first_transfer_position = upgrade.index(
        "ALTER FUNCTION "
        "app_private.handle_user_deactivation_cascade() "
        "OWNER TO app_rls_executor;"
    )
    assert preflight_position < prepare_position < first_transfer_position
    assert prepare_position < upgrade.index(
        "CREATE OR REPLACE FUNCTION "
        "app_private.handle_user_deactivation_cascade()"
    )

    normalized_upgrade_strings = [
        re.sub(r"\s+", " ", value).strip()
        for value in _string_constants(upgrade_node)
    ]
    index_statements = [
        value
        for value in normalized_upgrade_strings
        if value.startswith("CREATE ") and " INDEX " in value
    ]
    assert index_statements == [
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "ix_org_users_email_lower_active ON "
        "public.organization_users (email) "
        "WHERE (deleted_at IS NULL);",
        "CREATE INDEX IF NOT EXISTS ix_branch_staff_user_active ON "
        "public.branch_staff_roles (user_id, role) "
        "WHERE (deleted_at IS NULL AND revoked_at IS NULL);",
        "CREATE INDEX IF NOT EXISTS ix_branch_staff_branch_active ON "
        "public.branch_staff_roles (branch_id, role) "
        "WHERE (deleted_at IS NULL AND revoked_at IS NULL);",
    ]

    grant_usage_position = helper.index(
        "GRANT USAGE ON SCHEMA app_private"
    )
    grant_create_position = helper.index(
        "GRANT CREATE ON SCHEMA app_private"
    )
    prepare_definition_position = helper.index(
        "def _rb1l8d1a_prepare_upgrade_owner_context"
    )
    assert prepare_definition_position < grant_usage_position
    assert prepare_definition_position < grant_create_position


def test_app_private_owner_context_rejects_public_create_and_broad_role_escalation():
    helper = _helper_source()
    assert "acl_data.grantee = 0" in helper
    assert "acl_data.privilege_type = 'CREATE'" in helper
    assert "_rb1l8d1a_reject_public_create" in helper
    prohibited = (
        "SUPERUSER",
        "CREATEROLE",
        "BYPASSRLS",
        "ALTER SCHEMA app_private OWNER",
        "DROP OWNED",
        "acldefault(",
        "array_fill(",
        "ARRAY[]::pg_catalog.aclitem[]",
        "COALESCE(namespace_data.nspacl",
    )
    for token in prohibited:
        assert token not in helper
    assert "GRANT CREATE ON SCHEMA app_private TO PUBLIC" not in helper
    assert "REVOKE CREATE ON SCHEMA app_private FROM PUBLIC" not in helper


def test_app_private_owner_context_snapshots_create_and_usage_direct_acl_tuples():
    helper = _helper_source()
    assert "pg_catalog.aclexplode(" in helper
    assert "namespace_data.nspacl" in helper
    for field in (
        "grantor_name",
        "grantee_name",
        "privilege_type",
        "is_grantable",
    ):
        assert field in helper
    assert "acl_data.privilege_type IN ('CREATE', 'USAGE')" in helper
    assert "_rb1l8d1a_verify_exact_acl_rows" in helper
    assert "temporary CREATE grant" in helper
    assert "temporary USAGE grant" in helper
    assert "REVOKE CREATE ON SCHEMA app_private" in helper
    assert "REVOKE USAGE ON SCHEMA app_private" in helper
    assert "nspacl =" not in helper


def test_app_private_owner_context_validates_set_capability_and_principal_reset():
    source = _source()
    helper = _helper_source()
    prepare_node = _function(
        "_rb1l8d1a_prepare_upgrade_owner_context"
    )
    prepare = _function_source(
        "_rb1l8d1a_prepare_upgrade_owner_context"
    )
    upgrade_node = _function("upgrade")
    upgrade = _upgrade_source()
    assert "session_user::text AS session_user_name" in helper
    assert "current_user::text AS current_user_name" in helper
    assert "pg_catalog.pg_has_role(" in helper
    assert "'SET'" in helper
    assert "migration_owner lacks effective" in helper
    assert '"CREATE", "USAGE"' in helper
    assert helper.count('sa.text("SET LOCAL ROLE app_rls_executor")') == 2
    assert helper.count('sa.text("RESET ROLE")') == 2
    assert helper.count("_rb1l8d1a_require_migration_owner(bind)") >= 7
    target_function = "_rb1l8d1a_require_target_owner_capabilities"
    complete_calls = [
        item
        for item in ast.walk(_tree())
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == target_function
    ]
    prepare_calls = [
        item
        for item in ast.walk(prepare_node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == target_function
    ]
    upgrade_calls = [
        item
        for item in ast.walk(upgrade_node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == target_function
    ]
    assert len(complete_calls) == 2
    assert len(prepare_calls) == 0
    assert len(upgrade_calls) == 2
    assert (
        "_rb1l8d1a_require_target_owner_capabilities(bind)"
        not in prepare
    )
    assert (
        "_rb1l8d1a_require_target_owner_capabilities(bind)\n"
        '    op.execute("ALTER FUNCTION '
        "app_private.handle_user_deactivation_cascade() "
        'OWNER TO app_rls_executor;")'
    ) in upgrade
    assert (
        "_rb1l8d1a_require_target_owner_capabilities(bind)\n"
        '    op.execute("ALTER FUNCTION '
        "app_private.log_branch_staff_role_audit() "
        'OWNER TO app_rls_executor;")'
    ) in upgrade
    checks = _all_positions(
        upgrade,
        "_rb1l8d1a_require_target_owner_capabilities(bind)",
    )
    transfers = _all_positions(upgrade, "OWNER TO app_rls_executor;")
    assert len(checks) == 2
    assert len(transfers) == 2
    assert checks[0] < transfers[0] < checks[1] < transfers[1]


def test_app_private_owner_context_downgrade_uses_actual_object_owner_without_cascade():
    helper = _helper_source()
    drop_helper = _function_source("_rb1l8d1a_drop_owned_functions")
    downgrade = _function("downgrade")
    downgrade_strings = "\n".join(_string_constants(downgrade))
    assert "pg_catalog.to_regprocedure(:signature)" in helper
    assert "owner_role.rolname::text AS owner_name" in helper
    assert "Expected exactly one function" in helper
    assert "Unexpected owner" in helper
    assert "_rb1l8d1a_drop_owned_functions(_rb1l8d1a_bind())" in _source()
    assert "DROP FUNCTION IF EXISTS app_private.log_branch_staff_role_audit" not in downgrade_strings
    assert "DROP FUNCTION IF EXISTS app_private.handle_user_deactivation_cascade" not in downgrade_strings
    assert "CASCADE" not in drop_helper
    assert "_rb1l8d1a_require_migration_owner_schema_capabilities(bind)" not in drop_helper
    assert "GRANT CREATE ON SCHEMA app_private" not in drop_helper
    assert '"USAGE"' in drop_helper
    assert "GRANT USAGE ON SCHEMA app_private" in drop_helper
    assert "_rb1l8d1a_has_schema_privilege(" in drop_helper


def test_0021_owner_transfer_contract_covers_both_app_rls_executor_functions():
    upgrade = _function("upgrade")
    strings = _string_constants(upgrade)
    transfers = [
        re.sub(r"\s+", " ", value).strip()
        for value in strings
        if "OWNER TO app_rls_executor" in value
    ]
    assert transfers == [
        "ALTER FUNCTION app_private.handle_user_deactivation_cascade() OWNER TO app_rls_executor;",
        "ALTER FUNCTION app_private.log_branch_staff_role_audit() OWNER TO app_rls_executor;",
    ]
    upgrade_source = _upgrade_source()
    assert upgrade_source.count("CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()") == 1
    assert upgrade_source.count("CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()") == 1
    assert upgrade_source.count("CREATE TRIGGER trg_user_deactivation_cascade") == 1
    assert upgrade_source.count("CREATE TRIGGER trg_audit_branch_staff_roles") == 1
    assert upgrade_source.count("_rb1l8d1a_prepare_upgrade_owner_context(bind)") == 1
    assert upgrade_source.count("_rb1l8d1a_require_target_owner_capabilities(bind)") == 2
    assert upgrade_source.count("_rb1l8d1a_finalize_upgrade_owner_context(bind, owner_state)") == 1
    source = _source()
    section_nine = source.index(
        "# 9. Triggers were created before function ownership transfer"
    )
    downgrade_start = source.index("def downgrade():")
    assert "CREATE TRIGGER" not in source[section_nine:downgrade_start]


def test_0021_post_transfer_ddl_runs_under_app_rls_executor():
    source = _source()
    upgrade = _upgrade_source()
    assert source.count("_rb1l8d1a_execute_as_owner(") == 3
    first_create = upgrade.index(
        "CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()"
    )
    first_trigger = upgrade.index("CREATE TRIGGER trg_user_deactivation_cascade")
    first_check = upgrade.index(
        "_rb1l8d1a_require_target_owner_capabilities(bind)",
        first_trigger,
    )
    first_transfer = upgrade.index(
        "ALTER FUNCTION app_private.handle_user_deactivation_cascade() "
        "OWNER TO app_rls_executor;"
    )
    first_revoke = upgrade.index(
        '"app_private.handle_user_deactivation_cascade() "\n'
        '        "FROM PUBLIC"'
    )

    second_create = upgrade.index(
        "CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()"
    )
    second_trigger = upgrade.index("CREATE TRIGGER trg_audit_branch_staff_roles")
    second_check = upgrade.index(
        "_rb1l8d1a_require_target_owner_capabilities(bind)",
        second_trigger,
    )
    second_transfer = upgrade.index(
        "ALTER FUNCTION app_private.log_branch_staff_role_audit() "
        "OWNER TO app_rls_executor;"
    )
    second_revoke = upgrade.index(
        '"app_private.log_branch_staff_role_audit() "\n'
        '        "FROM PUBLIC"'
    )
    cleanup = upgrade.index(
        "_rb1l8d1a_finalize_upgrade_owner_context(bind, owner_state)"
    )

    assert first_create < first_trigger < first_check < first_transfer < first_revoke
    assert first_revoke < second_create
    assert second_create < second_trigger < second_check < second_transfer < second_revoke
    assert second_revoke < cleanup
    assert upgrade.count("FROM PUBLIC") == 2
    assert "GRANT EXECUTE ON FUNCTION" not in upgrade


def test_0021_downgrade_enters_app_rls_executor_for_both_function_drops():
    source = _source()
    helper = _helper_source()
    drop_helper = _function_source("_rb1l8d1a_drop_owned_functions")
    assert "_RB1L8D1A_FUNCTIONS = (" in helper
    assert '"app_private.handle_user_deactivation_cascade()"' in helper
    assert '"app_private.log_branch_staff_role_audit()"' in helper
    assert helper.count('sa.text("SET LOCAL ROLE app_rls_executor")') == 2
    assert (
        '"DROP FUNCTION "\n'
        '            "app_private.log_branch_staff_role_audit()"'
    ) in helper
    assert (
        '"DROP FUNCTION "\n'
        '            "app_private.handle_user_deactivation_cascade()"'
    ) in helper
    assert source.count("_rb1l8d1a_drop_owned_functions(_rb1l8d1a_bind())") == 1
    assert drop_helper.count("DROP FUNCTION") == 2
    assert "DROP FUNCTION IF EXISTS" not in drop_helper
    assert "GRANT CREATE" not in drop_helper
    assert "do not use cascade" not in helper.lower()

# RB1L8D1D2_APP_SECURE_OWNER_CONTEXT_REGRESSION

import ast as _rb1l8d1d2_ast
import hashlib as _rb1l8d1d2_hashlib
from pathlib import Path as _RB1L8D1D2Path

_RB1L8D1D2_ROOT = _RB1L8D1D2Path(__file__).resolve().parents[1]
_RB1L8D1D2_MIGRATION = (
    _RB1L8D1D2_ROOT
    / "alembic/versions/0022_rbac_phase1_roles_extensions.py"
)
_RB1L8D1D2_0021 = (
    _RB1L8D1D2_ROOT
    / "alembic/versions/0021_staff_roles.py"
)
_RB1L8D1D2_EXPECTED_0021_SHA = (
    "6078897c7bc82c14952af1c6e00e76d8ffde15ff19e1f0a73090076f3ada7e01"
)


def _rb1l8d1d2_source() -> str:
    return _RB1L8D1D2_MIGRATION.read_text(encoding="utf-8")


def _rb1l8d1d2_function(name: str) -> tuple[str, _rb1l8d1d2_ast.FunctionDef]:
    source = _rb1l8d1d2_source()
    tree = _rb1l8d1d2_ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, _rb1l8d1d2_ast.FunctionDef)
        and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1l8d1d2_segment(source: str, node: _rb1l8d1d2_ast.AST) -> str:
    value = _rb1l8d1d2_ast.get_source_segment(source, node)
    assert value is not None
    return value


def _rb1l8d1d2_literal(node: _rb1l8d1d2_ast.AST) -> str | None:
    try:
        value = _rb1l8d1d2_ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _rb1l8d1d2_run_as_sql(function_name: str) -> list[str]:
    source, function = _rb1l8d1d2_function(function_name)
    statements: list[str] = []
    for node in _rb1l8d1d2_ast.walk(function):
        if not isinstance(node, _rb1l8d1d2_ast.Call):
            continue
        if not isinstance(node.func, _rb1l8d1d2_ast.Name):
            continue
        if node.func.id != "_rb1l7_run_as" or len(node.args) != 3:
            continue
        role = _rb1l8d1d2_literal(node.args[1])
        sql = _rb1l8d1d2_literal(node.args[2])
        if role == "app_security_owner" and sql is not None:
            statements.append(" ".join(sql.split()))
    return statements


def _rb1l8d1d2_direct_app_secure_sql(function_name: str) -> list[str]:
    source, function = _rb1l8d1d2_function(function_name)
    statements: list[str] = []
    for node in _rb1l8d1d2_ast.walk(function):
        if not isinstance(node, _rb1l8d1d2_ast.Call):
            continue
        if not isinstance(node.func, _rb1l8d1d2_ast.Attribute):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        sql = _rb1l8d1d2_literal(node.args[0])
        if sql is not None and "app_secure" in sql.lower():
            statements.append(" ".join(sql.split()))
    return statements


def test_0022_app_secure_preflight_is_read_only_and_first_upgrade_action():
    source, preflight = _rb1l8d1d2_function(
        "_rb1l8d1d2_preflight_app_secure_owner_context"
    )
    preflight_source = _rb1l8d1d2_segment(source, preflight)
    assert "_rb1l7_require_migration_owner(bind)" in preflight_source
    assert "pg_catalog.pg_has_role" in preflight_source
    assert "pg_catalog.has_database_privilege" in preflight_source
    assert "pg_catalog.pg_get_userbyid" in preflight_source
    for forbidden in (
        "GRANT ",
        "REVOKE ",
        "CREATE SCHEMA",
        "ALTER ROLE",
        "DROP ROLE",
        "SET LOCAL ROLE",
        "RESET ROLE",
        "op.execute(",
    ):
        assert forbidden not in preflight_source

    upgrade_source, upgrade = _rb1l8d1d2_function("upgrade")
    first = _rb1l8d1d2_segment(upgrade_source, upgrade.body[0])
    second = _rb1l8d1d2_segment(upgrade_source, upgrade.body[1])
    assert first == "bind = _rb1l7_bind()"
    assert second == (
        "_rb1l8d1d2_preflight_app_secure_owner_context("
        "bind, require_schema=False)"
    )


def test_0022_app_secure_upgrade_owner_operations_are_bounded():
    direct = _rb1l8d1d2_direct_app_secure_sql("upgrade")
    assert len(direct) == 1
    assert "CREATE SCHEMA app_secure" in direct[0]
    assert "AUTHORIZATION app_security_owner" in direct[0]

    bounded = set(_rb1l8d1d2_run_as_sql("upgrade"))
    expected = {
        "REVOKE ALL ON SCHEMA app_secure FROM PUBLIC;",
        "GRANT USAGE ON SCHEMA app_secure TO app_runtime, readonly_analytics;",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure REVOKE ALL ON TABLES FROM PUBLIC;",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure GRANT SELECT ON TABLES TO app_runtime;",
    }
    assert expected.issubset(bounded)

    upgrade_source, upgrade = _rb1l8d1d2_function("upgrade")
    upgrade_text = _rb1l8d1d2_segment(upgrade_source, upgrade)
    create_at = upgrade_text.index("CREATE SCHEMA app_secure")
    assert_at = upgrade_text.index(
        "_rb1l8d1d2_assert_app_secure_owner(bind)"
    )
    first_bounded_at = upgrade_text.index(
        '_rb1l7_run_as(\n        bind,\n        "app_security_owner",'
    )
    assert create_at < assert_at < first_bounded_at


def test_0022_app_secure_downgrade_operations_are_bounded_and_reset():
    assert _rb1l8d1d2_direct_app_secure_sql("downgrade") == []
    bounded = set(_rb1l8d1d2_run_as_sql("downgrade"))
    assert "COMMENT ON SCHEMA app_secure IS NULL;" in bounded
    assert "DROP SCHEMA IF EXISTS app_secure CASCADE;" in bounded

    source, run_as = _rb1l8d1d2_function("_rb1l7_run_as")
    run_as_source = _rb1l8d1d2_segment(source, run_as)
    assert 'sa.text("RESET ROLE")' in run_as_source
    assert run_as_source.count("_rb1l7_require_migration_owner(bind)") >= 2

    downgrade_source, downgrade = _rb1l8d1d2_function("downgrade")
    first = _rb1l8d1d2_segment(downgrade_source, downgrade.body[0])
    second = _rb1l8d1d2_segment(downgrade_source, downgrade.body[1])
    assert first == "bind = _rb1l7_bind()"
    assert second == (
        "_rb1l8d1d2_preflight_app_secure_owner_context("
        "bind, require_schema=True)"
    )


def test_0022_app_secure_correction_preserves_contracts_and_0021():
    source = _rb1l8d1d2_source()
    assert "CREATE SCHEMA app_secure" in source
    assert "AUTHORIZATION app_security_owner" in source
    assert source.count(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure"
    ) >= 2
    assert "def _rb1l7_run_as(bind, role_name, sql):" in source
    assert "SET LOCAL ROLE " in source
    assert 'sa.text("RESET ROLE")' in source
    assert "_rb1l7_require_migration_owner(bind)" in source
    for forbidden in (
        "CREATE ROLE ",
        "ALTER ROLE ",
        "DROP ROLE ",
        "GRANT app_security_owner TO",
        "REVOKE app_security_owner FROM",
    ):
        assert forbidden not in source
    observed = _rb1l8d1d2_hashlib.sha256(
        _RB1L8D1D2_0021.read_bytes()
    ).hexdigest()
    assert observed == _RB1L8D1D2_EXPECTED_0021_SHA

# RB1M1A_0024_APP_PRIVATE_OWNER_CONTEXT_REGRESSION

import ast as _rb1m1a_ast
import hashlib as _rb1m1a_hashlib
from pathlib import Path as _RB1M1APath

_RB1M1A_ROOT = _RB1M1APath(__file__).resolve().parents[1]
_RB1M1A_0024 = _RB1M1A_ROOT / "alembic/versions/0024_rbac_p3_org_members.py"
_RB1M1A_0021 = _RB1M1A_ROOT / "alembic/versions/0021_staff_roles.py"
_RB1M1A_0022 = _RB1M1A_ROOT / "alembic/versions/0022_rbac_phase1_roles_extensions.py"
_RB1M1A_EXPECTED_0021_SHA = "6078897c7bc82c14952af1c6e00e76d8ffde15ff19e1f0a73090076f3ada7e01"
_RB1M1A_EXPECTED_0022_SHA = "c87022fe010522cc51b390ca1b9c28be95c3cd1cad99bf5027e7beeb1a7a5870"


def _rb1m1a_source() -> str:
    return _RB1M1A_0024.read_text(encoding="utf-8")


def _rb1m1a_function(name: str) -> tuple[str, _rb1m1a_ast.FunctionDef]:
    source = _rb1m1a_source()
    tree = _rb1m1a_ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, _rb1m1a_ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1m1a_segment(source: str, node: _rb1m1a_ast.AST) -> str:
    value = _rb1m1a_ast.get_source_segment(source, node)
    assert value is not None
    return value


def test_0024_upgrade_preflight_is_read_only_and_first_action():
    source, identity = _rb1m1a_function("_rb1m1a_identity")
    identity_text = _rb1m1a_segment(source, identity)
    for required in (
        "session_user::text",
        "current_user::text",
    ):
        assert required in identity_text

    source, require_owner = _rb1m1a_function(
        "_rb1m1a_require_migration_owner"
    )
    require_owner_text = _rb1m1a_segment(source, require_owner)
    assert "_rb1m1a_identity(bind)" in require_owner_text
    assert '!= "migration_owner"' in require_owner_text

    source, preflight = _rb1m1a_function("_rb1m1a_preflight")
    preflight_text = _rb1m1a_segment(source, preflight)
    for required in (
        "_rb1m1a_require_migration_owner(bind)",
        "schema_owner_name",
        "pg_catalog.pg_has_role",
        "migration_owner_can_create",
        "target_owner_can_use",
        "pg_catalog.to_regprocedure",
    ):
        assert required in preflight_text

    read_only_chain = "\n".join(
        (identity_text, require_owner_text, preflight_text)
    )
    for forbidden in (
        "GRANT ",
        "REVOKE ",
        "CREATE FUNCTION",
        "ALTER FUNCTION",
        "DROP FUNCTION",
        "SET LOCAL ROLE",
        "RESET ROLE",
        "op.execute(",
    ):
        assert forbidden not in read_only_chain

    upgrade_source, upgrade = _rb1m1a_function("upgrade")
    first = _rb1m1a_segment(upgrade_source, upgrade.body[0])
    second = _rb1m1a_segment(upgrade_source, upgrade.body[1])
    assert first == "bind = op.get_bind()"
    assert second == "_rb1m1a_preflight(bind, require_functions=False)"


def test_0024_both_functions_use_temporary_create_and_owner_transfer_contract():
    source, upgrade = _rb1m1a_function("upgrade")
    text = _rb1m1a_segment(source, upgrade)
    assert text.count("_rb1m1a_prepare_owner_transfer(bind)") == 1
    assert text.count(" OWNER TO app_security_owner;") == 2
    assert text.count("_rb1m1a_assert_function_owner(") == 2
    for signature in (
        "app_private.touch_updated_at()",
        "app_private.enforce_membership_state_transition()",
    ):
        assert signature in text

    _, prepare = _rb1m1a_function("_rb1m1a_prepare_owner_transfer")
    prepare_text = _rb1m1a_segment(source, prepare)
    assert "GRANT CREATE ON SCHEMA app_private" in prepare_text
    assert "TO app_security_owner" in prepare_text
    assert "has_schema_privilege" in prepare_text


def test_0024_exact_create_acl_prestate_is_restored_with_grantor_and_option():
    source, acl_rows = _rb1m1a_function("_rb1m1a_direct_create_acl_rows")
    acl_text = _rb1m1a_segment(source, acl_rows)
    for required in (
        "grantor_name",
        "grantee_name",
        "privilege_type",
        "is_grantable",
        "pg_catalog.aclexplode",
    ):
        assert required in acl_text

    _, restore = _rb1m1a_function("_rb1m1a_restore_owner_transfer")
    restore_text = _rb1m1a_segment(source, restore)
    assert "REVOKE CREATE ON SCHEMA app_private" in restore_text
    assert "FROM app_security_owner" in restore_text
    assert "state[\"before\"]" in restore_text
    assert "_rb1m1a_verify_create_acl_rows" in restore_text

    upgrade_source, upgrade = _rb1m1a_function("upgrade")
    upgrade_text = _rb1m1a_segment(upgrade_source, upgrade)
    last_transfer = upgrade_text.rindex(" OWNER TO app_security_owner;")
    restore_at = upgrade_text.index("_rb1m1a_restore_owner_transfer(")
    verify_at = upgrade_text.index("_rb1m1a_verify_function_contracts(bind)")
    assert last_transfer < restore_at < verify_at


def test_0024_public_revoke_comments_and_triggers_precede_each_transfer():
    source, upgrade = _rb1m1a_function("upgrade")
    text = _rb1m1a_segment(source, upgrade)
    contracts = (
        (
            "CREATE OR REPLACE FUNCTION app_private.touch_updated_at()",
            "REVOKE ALL ON FUNCTION app_private.touch_updated_at() FROM PUBLIC;",
            "COMMENT ON FUNCTION app_private.touch_updated_at()",
            "CREATE TRIGGER trg_touch_organization_members_updated_at",
            "ALTER FUNCTION app_private.touch_updated_at() OWNER TO app_security_owner;",
        ),
        (
            "CREATE OR REPLACE FUNCTION app_private.enforce_membership_state_transition()",
            "REVOKE ALL ON FUNCTION app_private.enforce_membership_state_transition() FROM PUBLIC;",
            "COMMENT ON FUNCTION app_private.enforce_membership_state_transition()",
            "CREATE TRIGGER trg_membership_state_transition",
            "ALTER FUNCTION app_private.enforce_membership_state_transition() OWNER TO app_security_owner;",
        ),
    )
    for contract in contracts:
        positions = [text.index(item) for item in contract]
        assert positions == sorted(positions)

    after_first_transfer = text[
        text.index(contracts[0][-1]) : text.index(contracts[1][0])
    ]
    after_second_transfer = text[
        text.index(contracts[1][-1]) : text.index("# ── 5. RLS policies")
    ]
    for segment in (after_first_transfer, after_second_transfer):
        assert "COMMENT ON FUNCTION" not in segment
        assert "REVOKE ALL ON FUNCTION" not in segment


def test_0024_downgrade_uses_bounded_owner_context_and_identity_reset():
    source, downgrade = _rb1m1a_function("downgrade")
    text = _rb1m1a_segment(source, downgrade)
    first = _rb1m1a_segment(source, downgrade.body[0])
    second = _rb1m1a_segment(source, downgrade.body[1])
    assert first == "bind = op.get_bind()"
    assert second == "_rb1m1a_preflight(bind, require_functions=True)"
    assert text.count("_rb1m1a_run_as_app_security_owner(") == 2
    assert "op.execute(\"DROP FUNCTION" not in text

    _, run_as = _rb1m1a_function("_rb1m1a_run_as_app_security_owner")
    run_as_text = _rb1m1a_segment(source, run_as)
    assert 'sa.text("SET LOCAL ROLE app_security_owner")' in run_as_text
    assert 'sa.text("RESET ROLE")' in run_as_text
    assert "finally:" in run_as_text
    assert run_as_text.count("_rb1m1a_require_migration_owner(bind)") >= 2


def test_0024_correction_preserves_protected_revisions_and_unrelated_semantics():
    source = _rb1m1a_source()
    assert _rb1m1a_hashlib.sha256(_RB1M1A_0021.read_bytes()).hexdigest() == _RB1M1A_EXPECTED_0021_SHA
    assert _rb1m1a_hashlib.sha256(_RB1M1A_0022.read_bytes()).hexdigest() == _RB1M1A_EXPECTED_0022_SHA
    for required in (
        "CREATE TABLE public.organization_members",
        "CONSTRAINT uq_org_member_user",
        "CONSTRAINT uq_org_member_pair",
        "CREATE INDEX ix_org_members_active",
        "CREATE INDEX ix_org_members_status",
        "SECURITY DEFINER",
        "SET search_path = pg_catalog",
        "CREATE POLICY tenant_isolation_organization_members",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "GRANT SELECT, INSERT, UPDATE ON public.organization_members",
    ):
        assert required in source
    for forbidden in (
        "CREATE ROLE ",
        "ALTER ROLE ",
        "DROP ROLE ",
        "GRANT app_security_owner TO",
        "GRANT migration_owner TO",
        "ALTER SCHEMA app_private OWNER",
        "DROP FUNCTION IF EXISTS app_private.touch_updated_at() CASCADE",
        "DROP FUNCTION IF EXISTS app_private.enforce_membership_state_transition() CASCADE",
    ):
        assert forbidden not in source

# RB1M2A_0025_COMPLETE_OWNER_CONTEXT_REGRESSION

import ast as _rb1m2a_ast
from pathlib import Path as _RB1M2APath

_RB1M2A_ROOT = _RB1M2APath(__file__).resolve().parents[1]
_RB1M2A_0025 = _RB1M2A_ROOT / "alembic/versions/0025_rbac_p4_bsr_expand.py"


def _rb1m2a_source() -> str:
    return _RB1M2A_0025.read_text(encoding="utf-8")


def _rb1m2a_function(name: str) -> tuple[str, _rb1m2a_ast.FunctionDef]:
    source = _rb1m2a_source()
    tree = _rb1m2a_ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, _rb1m2a_ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1m2a_segment(source: str, node: _rb1m2a_ast.AST) -> str:
    value = _rb1m2a_ast.get_source_segment(source, node)
    assert value is not None
    return value


def test_0025_preflight_is_read_only_and_first_upgrade_action():
    source, preflight = _rb1m2a_function("_rb1m2a_preflight")
    text = _rb1m2a_segment(source, preflight)
    for required in (
        "_rb1m2a_require_migration_owner",
        "_rb1m2a_can_set_role",
        "app_private",
        "app_secure",
        "PUBLIC CREATE on app_private is forbidden",
        "pg_catalog.to_regprocedure",
        "pg_catalog.to_regclass",
        "relation_kind",
        "owner_name",
        "_rb1m2a_direct_private_create_acl_rows",
        "_rb1m2a_direct_select_acl_rows",
        "_rb1m2a_validate_view_acl_state",
    ):
        assert required in text
    _, identity = _rb1m2a_function("_rb1m2a_identity")
    identity_text = _rb1m2a_segment(source, identity)
    assert "session_user::text AS session_user_name" in identity_text
    assert "current_user::text AS current_user_name" in identity_text
    _, can_set = _rb1m2a_function("_rb1m2a_can_set_role")
    assert "pg_catalog.pg_has_role" in _rb1m2a_segment(source, can_set)
    for forbidden in (
        "GRANT ",
        "REVOKE ",
        "CREATE TABLE",
        "CREATE FUNCTION",
        "CREATE OR REPLACE",
        "ALTER TABLE",
        "ALTER FUNCTION",
        "DROP TABLE",
        "DROP FUNCTION",
        "DROP VIEW",
        "SET LOCAL ROLE",
        "RESET ROLE",
        "op.execute(",
    ):
        assert forbidden not in text
    upgrade_source, upgrade = _rb1m2a_function("upgrade")
    first = _rb1m2a_segment(upgrade_source, upgrade.body[0])
    second = _rb1m2a_segment(upgrade_source, upgrade.body[1])
    assert first == "bind = op.get_bind()"
    assert second == "_rb1m2a_preflight(bind, require_objects=False)"


def test_0025_private_create_acl_snapshot_and_temporary_grant_cover_both_functions():
    source, acl = _rb1m2a_function("_rb1m2a_direct_private_create_acl_rows")
    acl_text = _rb1m2a_segment(source, acl)
    for required in (
        "grantor_oid",
        "grantor_name",
        "grantee_oid",
        "grantee_name",
        "privilege_type",
        "is_grantable",
        "pg_catalog.aclexplode",
    ):
        assert required in acl_text
    _, prepare = _rb1m2a_function("_rb1m2a_prepare_function_owner_transfer")
    prepare_text = _rb1m2a_segment(source, prepare)
    assert "GRANT CREATE ON SCHEMA app_private TO app_security_owner" in prepare_text
    assert "has_schema_privilege" in prepare_text
    _, restore = _rb1m2a_function("_rb1m2a_restore_function_owner_transfer")
    restore_text = _rb1m2a_segment(source, restore)
    assert "REVOKE CREATE ON SCHEMA app_private FROM app_security_owner" in restore_text
    assert 'state["before"]' in restore_text
    upgrade_source, upgrade = _rb1m2a_function("upgrade")
    upgrade_text = _rb1m2a_segment(upgrade_source, upgrade)
    assert upgrade_text.count("_rb1m2a_prepare_function_owner_transfer(bind)") == 1
    assert upgrade_text.count(" OWNER TO app_security_owner;") == 2
    assert upgrade_text.count("_rb1m2a_assert_function_contract(") == 2


def test_0025_both_function_sequences_are_safe_and_owner_asserted():
    source, upgrade = _rb1m2a_function("upgrade")
    text = _rb1m2a_segment(source, upgrade)
    contracts = (
        (
            "CREATE OR REPLACE FUNCTION app_private.validate_effective_from_window()",
            "REVOKE ALL ON FUNCTION app_private.validate_effective_from_window() FROM PUBLIC;",
            "CREATE TRIGGER trg_bsr_validate_effective_from",
            "ALTER FUNCTION app_private.validate_effective_from_window() OWNER TO app_security_owner;",
            '"app_private.validate_effective_from_window()",',
        ),
        (
            "CREATE OR REPLACE FUNCTION app_private.validate_rls_context_match()",
            "REVOKE ALL ON FUNCTION app_private.validate_rls_context_match() FROM PUBLIC;",
            "CREATE TRIGGER trg_bsr_validate_rls_context",
            "ALTER FUNCTION app_private.validate_rls_context_match() OWNER TO app_security_owner;",
            '"app_private.validate_rls_context_match()",',
        ),
    )
    for contract in contracts:
        positions = [text.index(token) for token in contract]
        assert positions == sorted(positions)
    restore_at = text.index("_rb1m2a_restore_function_owner_transfer(")
    assert restore_at > text.rindex(" OWNER TO app_security_owner;")
    assert text.index("_rb1m2a_verify_function_contracts(bind)") > restore_at


def test_0025_function_signatures_security_and_trigger_mappings_are_exact():
    source = _rb1m2a_source()
    for signature in (
        "app_private.validate_effective_from_window()",
        "app_private.validate_rls_context_match()",
    ):
        assert signature in source
    assert source.count("RETURNS TRIGGER") == 2
    assert source.count("STRICT") >= 2
    assert source.count("VOLATILE") >= 2
    assert source.count("PARALLEL UNSAFE") >= 2
    assert source.count("SECURITY DEFINER") >= 2
    assert source.count("SET search_path = pg_catalog") >= 2
    _, verify = _rb1m2a_function("_rb1m2a_assert_function_contract")
    verify_text = _rb1m2a_segment(source, verify)
    for required in (
        "prosecdef",
        "proisstrict",
        "provolatile",
        "proparallel",
        "search_path=pg_catalog",
        "public_execute_count",
        "trigger_data.tgname",
        "procedure_data.oid::regprocedure::text",
    ):
        assert required in verify_text
    for trigger_name in (
        "trg_bsr_validate_effective_from",
        "trg_bsr_validate_rls_context",
    ):
        assert trigger_name in source


def test_0025_app_secure_view_preflight_is_fail_closed():
    source, preflight = _rb1m2a_function("_rb1m2a_preflight")
    text = _rb1m2a_segment(source, preflight)
    for required in (
        "Required schema app_secure is absent",
        "app_secure must be owned by app_security_owner",
        "is not a normal view",
        "unauthorized owner",
        "Required app_secure.v_active_branch_staff_roles is absent during downgrade",
    ):
        assert required in text
    assert "relation_data.relkind" in text
    assert "owner_role.rolname" in text
    _, relation_owner = _rb1m2a_function("_rb1m2a_relation_owner")
    relation_text = _rb1m2a_segment(source, relation_owner)
    assert "Required base relation" in relation_text
    assert "unexpected relkind" in relation_text
    assert "Unapproved base-relation owner" in text


def test_0025_view_creation_and_hardening_use_bounded_security_owner_context():
    source, create_view = _rb1m2a_function("_rb1m2a_create_secure_view")
    text = _rb1m2a_segment(source, create_view)
    for required in (
        "CREATE OR REPLACE VIEW app_secure.v_active_branch_staff_roles",
        "security_barrier = true",
        "security_invoker = true",
        "REVOKE ALL ON app_secure.v_active_branch_staff_roles FROM PUBLIC",
        "TO app_runtime, readonly_analytics",
        "COMMENT ON VIEW app_secure.v_active_branch_staff_roles",
        "_rb1m2a_run_as_role(bind, _RB1M2A_TARGET_OWNER, statements)",
        "_rb1m2a_verify_view_contract(bind)",
    ):
        assert required in text
    _, verify_view = _rb1m2a_function("_rb1m2a_verify_view_contract")
    verify_view_text = _rb1m2a_segment(source, verify_view)
    assert "Unexpected view ACL contract" in verify_view_text
    assert "view_acl.grantee <> relation_data.relowner" in verify_view_text
    assert '"app_runtime"' in verify_view_text
    assert '"readonly_analytics"' in verify_view_text
    _, run_as = _rb1m2a_function("_rb1m2a_run_as_role")
    run_as_text = _rb1m2a_segment(source, run_as)
    assert 'sa.text("SET LOCAL ROLE app_security_owner")' in run_as_text
    assert 'sa.text("RESET ROLE")' in run_as_text
    assert "finally:" not in run_as_text
    assert run_as_text.count("_rb1m2a_require_migration_owner(bind)") >= 2


def test_0025_base_select_acl_state_is_exact_and_revision_scoped():
    source, prepare = _rb1m2a_function("_rb1m2a_prepare_view_acl_state")
    prepare_text = _rb1m2a_segment(source, prepare)
    for required in (
        "app_private.migration_0025_view_acl_state",
        "prestate_json JSONB NOT NULL",
        "added_by_revision BOOLEAN",
        "added_grantor_name",
        "GRANT SELECT ON",
        "TO app_security_owner",
        "Unexpected SELECT ACL delta",
        "Unexpected revision-added SELECT tuple",
    ):
        assert required in prepare_text
    _, validate = _rb1m2a_function("_rb1m2a_validate_view_acl_state")
    validate_text = _rb1m2a_segment(source, validate)
    for required in (
        "relation-set drift",
        "state table has unauthorized owner",
        "Captured SELECT ACL contract drift",
        "added_grantor_name",
        "prestate_json::text AS prestate_json_text",
        "json.loads",
    ):
        assert required in validate_text
    _, restore = _rb1m2a_function("_rb1m2a_restore_view_acl_state")
    restore_text = _rb1m2a_segment(source, restore)
    for required in (
        "added_by_revision",
        "REVOKE SELECT ON",
        "FROM app_security_owner",
        "Exact SELECT ACL restoration failed",
        "DROP TABLE app_private.migration_0025_view_acl_state RESTRICT",
    ):
        assert required in restore_text
    for forbidden in (
        "GRANT ALL",
        "REVOKE ALL ON public.branch_staff_roles",
        "ALTER SCHEMA app_secure OWNER",
        "ALTER SCHEMA app_private OWNER",
    ):
        assert forbidden not in source


def test_0025_downgrade_orders_view_acl_triggers_and_bounded_function_drops():
    source, downgrade = _rb1m2a_function("downgrade")
    text = _rb1m2a_segment(source, downgrade)
    first = _rb1m2a_segment(source, downgrade.body[0])
    second = _rb1m2a_segment(source, downgrade.body[1])
    assert first == "bind = op.get_bind()"
    assert second == "_rb1m2a_preflight(bind, require_objects=True)"
    ordered = (
        "_rb1m2a_drop_secure_view(bind)",
        "_rb1m2a_restore_view_acl_state(bind)",
        "DROP TRIGGER IF EXISTS trg_bsr_validate_rls_context",
        "DROP TRIGGER IF EXISTS trg_bsr_validate_effective_from",
        '_rb1m2a_drop_owned_function(\n        bind, "app_private.validate_rls_context_match()"',
        '_rb1m2a_drop_owned_function(\n        bind, "app_private.validate_effective_from_window()"',
        "DROP POLICY IF EXISTS tenant_isolation_staff_roles",
        "DROP COLUMN IF EXISTS organization_member_id",
    )
    positions = [text.index(token) for token in ordered]
    assert positions == sorted(positions)
    assert "DROP VIEW IF EXISTS" not in text
    assert "DROP FUNCTION IF EXISTS" not in text
    assert "CASCADE" not in text
    _, drop_view = _rb1m2a_function("_rb1m2a_drop_secure_view")
    assert "DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT" in _rb1m2a_segment(source, drop_view)
    _, drop_function = _rb1m2a_function("_rb1m2a_drop_owned_function")
    assert "DROP FUNCTION {signature} RESTRICT" in _rb1m2a_segment(source, drop_function)

# RB1M2C_0025_PROTECTED_SCHEMA_CATALOG_RESOLUTION_REGRESSION


def test_0025_app_secure_catalog_resolution_uses_direct_catalog_joins():
    source, preflight = _rb1m2a_function("_rb1m2a_preflight")
    preflight_text = _rb1m2a_segment(source, preflight)
    for required in (
        "JOIN pg_catalog.pg_namespace AS namespace_data",
        "namespace_data.nspname = 'app_secure'",
        "relation_data.relnamespace = namespace_data.oid",
        "relation_data.relname = 'v_active_branch_staff_roles'",
    ):
        assert required in preflight_text
    assert "'app_secure.v_active_branch_staff_roles'" not in preflight_text

    _, verify = _rb1m2a_function("_rb1m2a_verify_view_contract")
    verify_text = _rb1m2a_segment(source, verify)
    for required in (
        "relation_data.oid AS relation_oid",
        "JOIN pg_catalog.pg_namespace AS namespace_data",
        "namespace_data.nspname = 'app_secure'",
        "relation_data.relname = 'v_active_branch_staff_roles'",
    ):
        assert required in verify_text
    assert "pg_catalog.to_regclass" not in verify_text


def test_0025_view_privilege_checks_use_resolved_relation_oid():
    source, helper = _rb1m2a_function("_rb1m2a_has_table_privilege")
    helper_text = _rb1m2a_segment(source, helper)
    assert "CAST(:relation_oid AS oid)" in helper_text
    assert '"relation_oid": relation_oid' in helper_text
    assert ":relation_name" not in helper_text

    _, relation_oid = _rb1m2a_function("_rb1m2a_relation_oid")
    relation_oid_text = _rb1m2a_segment(source, relation_oid)
    assert "pg_catalog.pg_class" in relation_oid_text
    assert "pg_catalog.pg_namespace" in relation_oid_text
    assert "relation_data.oid AS relation_oid" in relation_oid_text

    _, prepare = _rb1m2a_function("_rb1m2a_prepare_view_acl_state")
    prepare_text = _rb1m2a_segment(source, prepare)
    assert "relation_oid = _rb1m2a_relation_oid(" in prepare_text
    assert "bind, _RB1M2A_TARGET_OWNER, relation_oid, \"SELECT\"" in prepare_text

    _, verify = _rb1m2a_function("_rb1m2a_verify_view_contract")
    verify_text = _rb1m2a_segment(source, verify)
    assert 'bind, grantee, view_relation_oid, "SELECT"' in verify_text
    assert "bind, grantee, _RB1M2A_VIEW, \"SELECT\"" not in verify_text


def test_0025_catalog_resolution_fix_preserves_least_privilege_owner_boundary():
    source = _rb1m2a_source()
    for forbidden in (
        "GRANT USAGE ON SCHEMA app_secure TO migration_owner",
        "GRANT CREATE ON SCHEMA app_secure TO migration_owner",
        "ALTER SCHEMA app_secure OWNER TO migration_owner",
        "ALTER SCHEMA app_secure OWNER TO app_security_owner",
    ):
        assert forbidden not in source
    _, run_as = _rb1m2a_function("_rb1m2a_run_as_role")
    run_as_text = _rb1m2a_segment(source, run_as)
    assert 'sa.text("SET LOCAL ROLE app_security_owner")' in run_as_text
    assert 'sa.text("RESET ROLE")' in run_as_text
    assert "finally:" not in run_as_text

# RB1M2E_0025_NATIVE_RELATION_OID_BIND_TYPE_REGRESSION


def test_0025_relation_oid_queries_preserve_native_asyncpg_integer_type():
    source = _rb1m2a_source()
    assert source.count("relation_data.oid AS relation_oid") == 2
    assert "relation_data.oid::text AS relation_oid" not in source

    _, relation_oid = _rb1m2a_function("_rb1m2a_relation_oid")
    relation_oid_text = _rb1m2a_segment(source, relation_oid)
    assert "SELECT relation_data.oid AS relation_oid" in relation_oid_text
    assert "return _rb1m2a_require_native_relation_oid(" in relation_oid_text

    _, verify = _rb1m2a_function("_rb1m2a_verify_view_contract")
    verify_text = _rb1m2a_segment(source, verify)
    assert "relation_data.oid AS relation_oid" in verify_text
    assert "view_relation_oid = _rb1m2a_require_native_relation_oid(" in verify_text
    assert 'bind, grantee, view_relation_oid, "SELECT"' in verify_text
    assert 'bind, grantee, row["relation_oid"], "SELECT"' not in verify_text


def test_0025_relation_oid_validation_is_fail_closed_without_type_coercion():
    source, helper = _rb1m2a_function("_rb1m2a_require_native_relation_oid")
    helper_text = _rb1m2a_segment(source, helper)
    for required in (
        "isinstance(relation_oid, bool)",
        "not isinstance(relation_oid, int)",
        "relation_oid <= 0",
        "return relation_oid",
        "non-native PostgreSQL relation OID",
        "invalid PostgreSQL relation OID",
    ):
        assert required in helper_text
    for forbidden in (
        "int(relation_oid)",
        "str(relation_oid)",
        "CAST(:relation_oid AS text)",
    ):
        assert forbidden not in helper_text
    assert source.count("_rb1m2a_require_native_relation_oid(") == 3
# RB1M2G_0025_DOWNGRADE_TRIGGER_MAPPING_PREFLIGHT_REGRESSION


def test_0025_downgrade_preflight_runs_complete_function_trigger_contract_verifier():
    source, preflight = _rb1m2a_function("_rb1m2a_preflight")
    preflight_text = _rb1m2a_segment(source, preflight)
    assert preflight_text.count("_rb1m2a_verify_function_contracts(bind)") == 1
    require_blocks = [
        node
        for node in preflight.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "require_objects"
    ]
    assert len(require_blocks) == 1
    verifier_calls = [
        node
        for node in ast.walk(require_blocks[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_rb1m2a_verify_function_contracts"
    ]
    assert len(verifier_calls) == 1
    assert preflight_text.index("_rb1m2a_require_migration_owner(bind)") < preflight_text.index(
        "_rb1m2a_verify_function_contracts(bind)"
    )

    _, downgrade = _rb1m2a_function("downgrade")
    downgrade_text = _rb1m2a_segment(source, downgrade)
    preflight_index = downgrade_text.index(
        "_rb1m2a_preflight(bind, require_objects=True)"
    )
    destructive_tokens = (
        "_rb1m2a_restore_view_acl_state(",
        "_rb1m2a_restore_function_owner_transfer(",
        "DROP VIEW",
        "DROP TRIGGER",
        "DROP FUNCTION",
        "DROP POLICY",
        "DROP INDEX",
        "DROP CONSTRAINT",
        "DROP COLUMN",
    )
    destructive_indexes = [
        downgrade_text.index(token)
        for token in destructive_tokens
        if token in downgrade_text
    ]
    assert destructive_indexes
    assert preflight_index < min(destructive_indexes)


def test_0025_downgrade_preflight_reuses_exact_trigger_mapping_contract_without_upgrade_regression():
    source, preflight = _rb1m2a_function("_rb1m2a_preflight")
    preflight_text = _rb1m2a_segment(source, preflight)
    assert preflight_text.count("if require_objects:") >= 1
    assert preflight_text.count("_rb1m2a_verify_function_contracts(bind)") == 1
    require_blocks = [
        node
        for node in preflight.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "require_objects"
    ]
    assert len(require_blocks) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_rb1m2a_verify_function_contracts"
        for node in ast.walk(require_blocks[0])
    )

    tree = ast.parse(source)
    trigger_map_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_RB1M2A_TRIGGER_MAP"
    ]
    assert len(trigger_map_assignments) == 1
    assert ast.literal_eval(trigger_map_assignments[0].value) == (
        (
            "trg_bsr_validate_effective_from",
            "app_private.validate_effective_from_window()",
        ),
        (
            "trg_bsr_validate_rls_context",
            "app_private.validate_rls_context_match()",
        ),
    )

    _, verifier = _rb1m2a_function("_rb1m2a_verify_function_contracts")
    verifier_text = _rb1m2a_segment(source, verifier)
    assert "for trigger_name, signature in _RB1M2A_TRIGGER_MAP:" in verifier_text
    assert (
        "_rb1m2a_assert_function_contract(bind, signature, trigger_name)"
        in verifier_text
    )

    _, upgrade = _rb1m2a_function("upgrade")
    upgrade_text = _rb1m2a_segment(source, upgrade)
    assert "_rb1m2a_preflight(bind, require_objects=False)" in upgrade_text
    assert upgrade_text.count("_rb1m2a_verify_function_contracts(bind)") == 1
    assert source.count("_rb1m2a_verify_function_contracts(bind)") == 3

# RB1M2I_0025_ABORTED_TRANSACTION_ROLE_CLEANUP_REGRESSION


def test_0025_bounded_role_cleanup_runs_only_after_successful_protected_ddl():
    source, helper = _rb1m2a_function("_rb1m2a_run_as_role")
    helper_text = _rb1m2a_segment(source, helper)

    assert "finally:" not in helper_text
    assert "InFailedSQLTransactionError" in helper_text
    assert "SET LOCAL ROLE is transaction-scoped" in helper_text
    assert helper_text.count('sa.text("RESET ROLE")') == 1

    reset_index = helper_text.index('bind.execute(sa.text("RESET ROLE"))')
    protected_loop_indexes = [
        helper_text.index(_rb1m2a_segment(source, node))
        for node in helper.body
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "statements"
    ]
    assert len(protected_loop_indexes) == 1
    assert protected_loop_indexes[0] < reset_index
    assert reset_index < helper_text.rindex("_rb1m2a_require_migration_owner(bind)")
    assert not any(isinstance(node, ast.Try) for node in ast.walk(helper))


def test_0025_bounded_role_helper_preserves_original_failure_and_resets_on_success():
    source, helper = _rb1m2a_function("_rb1m2a_run_as_role")
    helper_text = _rb1m2a_segment(source, helper)

    class _Text:
        def __init__(self, value):
            self.value = value

    class _SA:
        @staticmethod
        def text(value):
            return _Text(value)

    class _OriginalFailure(RuntimeError):
        pass

    class _Bind:
        def __init__(self, failure=None):
            self.events = []
            self.failure = failure

        def execute(self, text):
            self.events.append(f"sql:{text.value}")
            if self.failure is not None and text.value == "PROTECTED DDL":
                raise self.failure
            return None

    def _require(bind):
        bind.events.append("require:migration_owner")

    def _can_set(bind, role_name):
        bind.events.append(f"can-set:{role_name}")
        return True

    def _identity(bind):
        bind.events.append("identity")
        return {
            "session_user_name": "migration_owner",
            "current_user_name": "app_security_owner",
        }

    namespace = {
        "sa": _SA,
        "_RB1M2A_ALLOWED_SET_ROLES": (
            "migration_owner",
            "app_security_owner",
            "app_rls_executor",
        ),
        "_rb1m2a_require_migration_owner": _require,
        "_rb1m2a_can_set_role": _can_set,
        "_rb1m2a_identity": _identity,
    }
    exec(helper_text, namespace)
    run_as_role = namespace["_rb1m2a_run_as_role"]

    successful = _Bind()
    run_as_role(successful, "app_security_owner", "PROTECTED DDL")
    assert successful.events == [
        "require:migration_owner",
        "can-set:app_security_owner",
        "sql:SET LOCAL ROLE app_security_owner",
        "identity",
        "sql:PROTECTED DDL",
        "sql:RESET ROLE",
        "require:migration_owner",
    ]

    original = _OriginalFailure("original dependency error")
    failing = _Bind(original)
    try:
        run_as_role(failing, "app_security_owner", "PROTECTED DDL")
    except _OriginalFailure as observed:
        assert observed is original
    else:  # pragma: no cover - assertion branch
        raise AssertionError("Original protected-DDL failure did not propagate.")

    assert failing.events == [
        "require:migration_owner",
        "can-set:app_security_owner",
        "sql:SET LOCAL ROLE app_security_owner",
        "identity",
        "sql:PROTECTED DDL",
    ]
    assert "sql:RESET ROLE" not in failing.events

# RB1M2L_0026_TRIGGER_CLONE_AUTHORITY_AND_DOWNGRADE_RESTORATION_REGRESSION

import ast as _rb1m2l_ast
from pathlib import Path as _RB1M2LPath

_RB1M2L_ROOT = _RB1M2LPath(__file__).resolve().parents[1]
_RB1M2L_0026 = _RB1M2L_ROOT / "alembic/versions/0026_rbac_p5_audit_log.py"


def _rb1m2l_source() -> str:
    return _RB1M2L_0026.read_text(encoding="utf-8")


def _rb1m2l_function(name: str) -> tuple[str, _rb1m2l_ast.FunctionDef]:
    source = _rb1m2l_source()
    matches = [
        node
        for node in _rb1m2l_ast.parse(source).body
        if isinstance(node, _rb1m2l_ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1m2l_segment(source: str, node: _rb1m2l_ast.AST) -> str:
    value = _rb1m2l_ast.get_source_segment(source, node)
    assert value is not None
    return value


def _rb1m2l_literal_sql(name: str) -> list[str]:
    source, function = _rb1m2l_function(name)
    statements: list[str] = []
    for node in _rb1m2l_ast.walk(function):
        if not isinstance(node, _rb1m2l_ast.Call):
            continue
        if not isinstance(node.func, _rb1m2l_ast.Attribute):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        try:
            value = _rb1m2l_ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            statements.append(" ".join(value.split()))
    return statements


def test_0026_trigger_clone_authority_is_preserved_until_seed_partitions_exist():
    source, upgrade = _rb1m2l_function("upgrade")
    text = _rb1m2l_segment(source, upgrade)
    ordered = (
        "CREATE OR REPLACE FUNCTION app_private.raise_immutable_audit_violation()",
        "REVOKE ALL ON FUNCTION app_private.raise_immutable_audit_violation() FROM PUBLIC;",
        "CREATE TRIGGER trg_deny_audit_mutation",
        "CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m06",
        "CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m07",
        "CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m08",
        "ALTER FUNCTION app_private.raise_immutable_audit_violation() OWNER TO app_security_owner;",
        "ALTER TABLE public.branch_audit_log ENABLE ROW LEVEL SECURITY;",
    )
    positions = [text.index(token) for token in ordered]
    assert positions == sorted(positions)
    assert text.count(ordered[-2]) == 1


def test_0026_trigger_clone_fix_uses_no_persistent_execute_workaround():
    source = " ".join(_rb1m2l_source().split())
    forbidden = (
        "GRANT EXECUTE ON FUNCTION app_private.raise_immutable_audit_violation() TO migration_owner",
        "GRANT EXECUTE ON FUNCTION app_private.raise_immutable_audit_violation() TO postgres",
        "GRANT EXECUTE ON FUNCTION app_private.raise_immutable_audit_violation() TO PUBLIC",
        "GRANT EXECUTE ON FUNCTION app_private.raise_immutable_audit_violation() TO app_runtime",
        "GRANT EXECUTE ON FUNCTION app_private.ensure_future_partition(TEXT, INT) TO postgres",
    )
    for token in forbidden:
        assert token not in source


def test_0026_partition_lifecycle_function_is_hardened_before_owner_handoff():
    source, upgrade = _rb1m2l_function("upgrade")
    text = _rb1m2l_segment(source, upgrade)
    create = text.index("CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(")
    revoke = text.index(
        "REVOKE ALL ON FUNCTION app_private.ensure_future_partition(TEXT, INT) FROM PUBLIC;"
    )
    owner = text.index(
        "ALTER FUNCTION app_private.ensure_future_partition(TEXT, INT) OWNER TO app_security_owner;"
    )
    seed = text.index("CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m06")
    assert create < revoke < owner < seed


def test_0026_downgrade_restores_rls_force_state_and_exact_table_grants():
    statements = _rb1m2l_literal_sql("downgrade")
    ordered = (
        "DROP POLICY IF EXISTS tenant_isolation_audit_log ON public.branch_audit_log;",
        "ALTER TABLE public.branch_audit_log NO FORCE ROW LEVEL SECURITY;",
        "REVOKE INSERT, SELECT ON public.branch_audit_log FROM audit_writer;",
        "REVOKE SELECT ON public.branch_audit_log FROM app_runtime, readonly_analytics;",
        "DROP TRIGGER IF EXISTS trg_deny_audit_mutation ON public.branch_audit_log;",
    )
    for token in ordered:
        assert statements.count(token) == 1
    positions = [statements.index(token) for token in ordered]
    assert positions == sorted(positions)
    assert all("DISABLE ROW LEVEL SECURITY" not in item for item in statements)


def test_0026_downgrade_removes_seeded_partitions_with_restrict_before_functions():
    statements = _rb1m2l_literal_sql("downgrade")
    partitions = (
        "DROP TABLE IF EXISTS public.branch_audit_log_y2026_m06 RESTRICT;",
        "DROP TABLE IF EXISTS public.branch_audit_log_y2026_m07 RESTRICT;",
        "DROP TABLE IF EXISTS public.branch_audit_log_y2026_m08 RESTRICT;",
    )
    for token in partitions:
        assert statements.count(token) == 1
    set_role = statements.index("SET LOCAL ROLE app_security_owner;")
    first_function = statements.index(
        "DROP FUNCTION IF EXISTS app_private.ensure_future_partition(TEXT, INT);"
    )
    assert statements.index(partitions[0]) < statements.index(partitions[1])
    assert statements.index(partitions[1]) < statements.index(partitions[2])
    assert statements.index(partitions[2]) < set_role < first_function


def test_0026_downgrade_security_owner_context_is_exact_bounded_and_success_only():
    source, downgrade = _rb1m2l_function("downgrade")
    text = _rb1m2l_segment(source, downgrade)
    statements = _rb1m2l_literal_sql("downgrade")
    assert statements.count("SET LOCAL ROLE app_security_owner;") == 1
    assert statements.count("RESET ROLE;") == 1
    start = statements.index("SET LOCAL ROLE app_security_owner;")
    end = statements.index("RESET ROLE;")
    assert start < end
    owner_block = statements[start + 1 : end]
    required = (
        "app_private.ensure_future_partition(TEXT, INT)",
        "app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID)",
        "app_private.org_advisory_lock_key(UUID)",
        "app_private.raise_immutable_audit_violation()",
    )
    assert len(owner_block) == 4
    for token in required:
        assert any(token in item for item in owner_block)
    assert "finally:" not in text
    assert not any(isinstance(node, _rb1m2l_ast.Try) for node in _rb1m2l_ast.walk(downgrade))


def test_0026_seed_partition_contract_is_exactly_three_revision_scoped_objects():
    upgrade = _rb1m2l_literal_sql("upgrade")
    downgrade = _rb1m2l_literal_sql("downgrade")
    for suffix in ("m06", "m07", "m08"):
        create_token = f"CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_{suffix} PARTITION OF public.branch_audit_log"
        drop_token = f"DROP TABLE IF EXISTS public.branch_audit_log_y2026_{suffix} RESTRICT;"
        assert sum(create_token in item for item in upgrade) == 1
        assert downgrade.count(drop_token) == 1
    assert not any("DROP TABLE" in item and "public.branch_audit_log " in item for item in downgrade)


def test_0026_upgrade_and_downgrade_preserve_fail_closed_audit_boundaries():
    source = _rb1m2l_source()
    upgrade = _rb1m2l_literal_sql("upgrade")
    downgrade = _rb1m2l_literal_sql("downgrade")
    assert upgrade.count("ALTER TABLE public.branch_audit_log ENABLE ROW LEVEL SECURITY;") == 1
    assert upgrade.count("ALTER TABLE public.branch_audit_log FORCE ROW LEVEL SECURITY;") == 1
    assert any("CREATE POLICY tenant_isolation_audit_log" in item for item in upgrade)
    assert "REVOKE UPDATE, DELETE ON public.branch_audit_log FROM app_runtime;" in upgrade
    for forbidden in (
        "DROP TABLE public.branch_audit_log",
        "DROP TABLE IF EXISTS public.branch_audit_log CASCADE",
        "REVOKE ALL ON public.branch_audit_log",
        "GRANT UPDATE, DELETE ON public.branch_audit_log TO app_runtime",
    ):
        assert forbidden not in source
    assert not any("CASCADE" in item for item in downgrade)

# RB1M2N_0026_APP_PRIVATE_FUNCTION_OWNER_TRANSFER_SCHEMA_CREATE_ACL_REGRESSION

import ast as _rb1m2n_ast
from pathlib import Path as _RB1M2NPath

_RB1M2N_ROOT = _RB1M2NPath(__file__).resolve().parents[1]
_RB1M2N_0026 = _RB1M2N_ROOT / "alembic/versions/0026_rbac_p5_audit_log.py"


def _rb1m2n_source() -> str:
    return _RB1M2N_0026.read_text(encoding="utf-8")


def _rb1m2n_function(name: str) -> tuple[str, _rb1m2n_ast.FunctionDef]:
    source = _rb1m2n_source()
    matches = [
        node
        for node in _rb1m2n_ast.parse(source).body
        if isinstance(node, _rb1m2n_ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1m2n_segment(source: str, node: _rb1m2n_ast.AST) -> str:
    value = _rb1m2n_ast.get_source_segment(source, node)
    assert value is not None
    return value


def test_0026_owner_transfer_preflight_is_identity_schema_and_role_aware():
    source, function = _rb1m2n_function(
        "_rb1m2n_preflight_private_owner_transfer"
    )
    text = _rb1m2n_segment(source, function)
    for token in (
        "_rb1m2n_require_migration_owner(bind)",
        "namespace_data.nspname = 'app_private'",
        'schema_row["owner_name"] != "migration_owner"',
        "rolname = 'app_security_owner'",
        "pg_catalog.pg_has_role(",
        "'SET'",
        "'USAGE'",
    ):
        assert token in text


def test_0026_owner_transfer_captures_exact_direct_create_acl_rows():
    source, function = _rb1m2n_function(
        "_rb1m2n_direct_private_create_acl"
    )
    text = _rb1m2n_segment(source, function)
    for token in (
        "pg_catalog.aclexplode(",
        "pg_catalog.acldefault('n', namespace_data.nspowner)",
        "grantor_name",
        "grantee_name",
        "privilege_type",
        "is_grantable",
        "schema_acl.privilege_type = 'CREATE'",
    ):
        assert token in text


def test_0026_owner_transfer_grants_only_missing_effective_create():
    source, function = _rb1m2n_function(
        "_rb1m2n_prepare_private_create_acl"
    )
    text = _rb1m2n_segment(source, function)
    assert "added = has_create is not True" in text
    assert "GRANT CREATE ON SCHEMA app_private" in text
    assert "TO app_security_owner" in text
    assert "GRANT ALL" not in text
    assert "GRANT USAGE, CREATE" not in text


def test_0026_upgrade_opens_acl_window_before_first_revision_mutation():
    source, upgrade = _rb1m2n_function("upgrade")
    text = _rb1m2n_segment(source, upgrade)
    prepare = text.index("_rb1m2n_prepare_private_create_acl(bind)")
    first_mutation = text.index(
        'op.execute("CREATE SEQUENCE IF NOT EXISTS public.branch_audit_log_seq;")'
    )
    assert prepare < first_mutation


def test_0026_all_four_owner_transfers_are_inside_one_exact_acl_window():
    source, upgrade = _rb1m2n_function("upgrade")
    text = _rb1m2n_segment(source, upgrade)
    prepare = text.index("_rb1m2n_prepare_private_create_acl(bind)")
    transfers = (
        "ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;",
        "ALTER FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) OWNER TO app_security_owner;",
        "ALTER FUNCTION app_private.ensure_future_partition(TEXT, INT) OWNER TO app_security_owner;",
        "ALTER FUNCTION app_private.raise_immutable_audit_violation() OWNER TO app_security_owner;",
    )
    positions = [text.index(token) for token in transfers]
    restore = text.index("_rb1m2n_restore_private_create_acl(")
    verify = text.index("_rb1m2n_verify_function_owner_contract(bind)")
    assert prepare < min(positions)
    assert max(positions) < restore < verify
    for token in transfers:
        assert text.count(token) == 1


def test_0026_acl_restoration_is_conditional_and_exact():
    source, function = _rb1m2n_function(
        "_rb1m2n_restore_private_create_acl"
    )
    text = _rb1m2n_segment(source, function)
    assert "if added:" in text
    assert "REVOKE CREATE ON SCHEMA app_private" in text
    assert "FROM app_security_owner" in text
    assert "observed = _rb1m2n_direct_private_create_acl(bind)" in text
    assert "if observed != before:" in text
    assert "REVOKE ALL ON SCHEMA" not in text


def test_0026_owner_contract_verifies_all_target_owners_and_public_execute():
    source, function = _rb1m2n_function(
        "_rb1m2n_verify_function_owner_contract"
    )
    text = _rb1m2n_segment(source, function)
    for token in (
        "append_audit_event",
        "ensure_future_partition",
        "org_advisory_lock_key",
        "raise_immutable_audit_violation",
        "owner_role.rolname",
        "function_acl.grantee = 0",
        "function_acl.privilege_type = 'EXECUTE'",
        "_RB1M2N_TARGET_OWNER",
    ):
        assert token in text


def test_0026_owner_transfer_acl_cleanup_is_success_path_only_and_least_privilege():
    source, upgrade = _rb1m2n_function("upgrade")
    upgrade_text = _rb1m2n_segment(source, upgrade)
    assert not any(
        isinstance(node, _rb1m2n_ast.Try)
        for node in _rb1m2n_ast.walk(upgrade)
    )
    assert "finally:" not in upgrade_text
    whole = " ".join(source.split())
    for forbidden in (
        "ALTER SCHEMA app_private OWNER TO app_security_owner",
        "ALTER SCHEMA app_private OWNER TO migration_owner",
        "GRANT CREATE ON SCHEMA app_private TO PUBLIC",
        "GRANT ALL ON SCHEMA app_private",
        "GRANT CREATE ON SCHEMA app_private TO app_runtime",
        "GRANT CREATE ON SCHEMA app_private TO postgres",
    ):
        assert forbidden not in whole

# RB1M2P_0026_PRE_TRANSFER_FUNCTION_ACL_HARDENING_REGRESSION

import ast as _rb1m2p_ast
from pathlib import Path as _RB1M2PPath

_RB1M2P_ROOT = _RB1M2PPath(__file__).resolve().parents[1]
_RB1M2P_0026 = _RB1M2P_ROOT / "alembic/versions/0026_rbac_p5_audit_log.py"


def _rb1m2p_source() -> str:
    return _RB1M2P_0026.read_text(encoding="utf-8")


def _rb1m2p_function(name: str) -> tuple[str, _rb1m2p_ast.FunctionDef]:
    source = _rb1m2p_source()
    matches = [
        node
        for node in _rb1m2p_ast.parse(source).body
        if isinstance(node, _rb1m2p_ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return source, matches[0]


def _rb1m2p_segment(source: str, node: _rb1m2p_ast.AST) -> str:
    value = _rb1m2p_ast.get_source_segment(source, node)
    assert value is not None
    return value


def test_0026_org_advisory_lock_acl_is_hardened_before_owner_transfer():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    revoke = text.index(
        "REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;"
    )
    grant = text.index(
        "GRANT EXECUTE ON FUNCTION app_private.org_advisory_lock_key(UUID) TO audit_writer;"
    )
    transfer = text.index(
        "ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;"
    )
    assert revoke < grant < transfer


def test_0026_append_audit_event_acl_is_hardened_before_owner_transfer():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    signature = (
        "app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,"
        "UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID)"
    )
    revoke = text.index(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
    grant = text.index(f"GRANT EXECUTE ON FUNCTION {signature} TO audit_writer;")
    transfer = text.index(f"ALTER FUNCTION {signature} OWNER TO app_security_owner;")
    assert revoke < grant < transfer


def test_0026_org_advisory_lock_has_no_post_owner_acl_mutation():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    transfer = text.index(
        "ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;"
    )
    tail = text[transfer:]
    assert (
        "REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;"
        not in tail
    )
    assert (
        "GRANT EXECUTE ON FUNCTION app_private.org_advisory_lock_key(UUID) TO audit_writer;"
        not in tail
    )


def test_0026_append_audit_event_has_no_post_owner_acl_mutation():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    signature = (
        "app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,"
        "UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID)"
    )
    transfer = text.index(f"ALTER FUNCTION {signature} OWNER TO app_security_owner;")
    tail = text[transfer:]
    assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;" not in tail
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO audit_writer;" not in tail


def test_0026_audit_writer_receives_only_explicit_execute_on_writer_functions():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    assert text.count(
        "GRANT EXECUTE ON FUNCTION app_private.org_advisory_lock_key(UUID) TO audit_writer;"
    ) == 1
    assert text.count(
        "GRANT EXECUTE ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) TO audit_writer;"
    ) == 1
    assert "GRANT ALL ON FUNCTION" not in text
    assert "GRANT EXECUTE ON ALL FUNCTIONS" not in text


def test_0026_public_execute_verifier_uses_effective_default_acl_semantics():
    source, function = _rb1m2p_function(
        "_rb1m2n_verify_function_owner_contract"
    )
    text = _rb1m2p_segment(source, function)
    assert "COALESCE(" in text
    assert "procedure_data.proacl" in text
    assert "pg_catalog.acldefault('f', procedure_data.proowner)" in text
    assert "function_acl.grantee = 0" in text
    assert "function_acl.privilege_type = 'EXECUTE'" in text


def test_0026_function_acl_hardening_remains_inside_schema_create_window():
    source, upgrade = _rb1m2p_function("upgrade")
    text = _rb1m2p_segment(source, upgrade)
    prepare = text.index("_rb1m2n_prepare_private_create_acl(bind)")
    org_revoke = text.index(
        "REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;"
    )
    append_revoke = text.index(
        "REVOKE ALL ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) FROM PUBLIC;"
    )
    restore = text.index("_rb1m2n_restore_private_create_acl(")
    assert prepare < org_revoke < restore
    assert prepare < append_revoke < restore


def test_0026_warning_prone_post_transfer_acl_patterns_are_absent():
    compact = " ".join(_rb1m2p_source().split())
    forbidden = (
        "op.execute(\"ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;\") "
        "op.execute(\"REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;\")",
        "op.execute(\"ALTER FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) OWNER TO app_security_owner;\") "
        "op.execute(\"REVOKE ALL ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) FROM PUBLIC;\")",
    )
    for token in forbidden:
        assert token not in compact

# RB1M2W_0028_RB1L7_COMPATIBLE_OWNER_CONTEXT_REGRESSION

import ast as _rb1m2w_ast
from pathlib import Path as _RB1M2WPath

_RB1M2W_ROOT = _RB1M2WPath(__file__).resolve().parents[1]
_RB1M2W_0028 = (
    _RB1M2W_ROOT
    / "alembic/versions/0028_rbac_p7_role_events.py"
)


def _rb1m2w_source() -> str:
    return _RB1M2W_0028.read_text(encoding="utf-8")


def _rb1m2w_function(name: str) -> str:
    source = _rb1m2w_source()
    tree = _rb1m2w_ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, _rb1m2w_ast.FunctionDef)
        and node.name == name
    ]
    assert len(matches) == 1
    segment = _rb1m2w_ast.get_source_segment(source, matches[0])
    assert segment is not None
    return segment


def test_0028_rb1l7_preflight_and_ephemeral_owner_window_are_first():
    upgrade = _rb1m2w_function("upgrade")
    assert upgrade.index("bind = _rb1l7_bind()") < upgrade.index(
        "_rb1m2w_preflight_upgrade(bind)"
    )
    assert upgrade.index("_rb1m2w_preflight_upgrade(bind)") < upgrade.index(
        "owner_state = _rb1m2w_prepare_owner_context(bind)"
    )
    assert upgrade.index("_rb1m2w_prepare_owner_context(bind)") < upgrade.index(
        "CREATE TABLE public.role_permission_events"
    )

    preflight = _rb1m2w_function("_rb1m2w_preflight_upgrade")
    for required in (
        "_rb1m2w_require_migration_owner",
        "_rb1m2w_can_set_security_owner",
        "PUBLIC CREATE on",
        "CREATE WITH GRANT OPTION",
        "role_permission_events",
        "effective_role_permissions",
        "_RB1M2W_COMPILE_FUNCTION",
    ):
        assert required in preflight
    for forbidden in (
        "GRANT CREATE ON SCHEMA",
        "REVOKE CREATE ON SCHEMA",
        "ALTER FUNCTION",
        "ALTER TABLE",
        "SET LOCAL ROLE",
        "RESET ROLE",
        "bind.execute(",
        "op.execute(",
    ):
        assert forbidden not in preflight


def test_0028_rb1l7_marker_remains_single_purpose_and_create_is_ephemeral():
    source = _rb1m2w_source()
    assert (
        "_RB1L7_ACL_OPERATIONS = "
        "(('GRANT', 'public', 'app_security_owner', 'USAGE'),)"
        in source
    )
    rb1l7_region = source[
        source.index("# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START"):
        source.index("# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_END")
    ]
    assert "'CREATE'" not in rb1l7_region[
        rb1l7_region.index("_RB1L7_ACL_OPERATIONS"):
        rb1l7_region.index("def _rb1l7_bind")
    ]
    assert source.count("migration_0028_schema_acl_state") >= 1
    assert source.count("# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START") == 1
    assert source.count("# RB1M2W_0028_COMPLETE_OWNER_CONTEXT_HELPERS_START") == 1

    prepare = _rb1m2w_function("_rb1m2w_prepare_owner_context")
    restore = _rb1m2w_function("_rb1m2w_restore_owner_context")
    assert 'f"GRANT CREATE ON SCHEMA {schema_name} "' in prepare
    assert '"TO app_security_owner"' in prepare
    assert 'f"REVOKE CREATE ON SCHEMA {schema_name} "' in restore
    assert '"FROM app_security_owner"' in restore
    assert "schema ACL restoration failed" in restore


def test_0028_all_function_dependencies_precede_owner_transfer():
    upgrade = _rb1m2w_function("upgrade")

    ordered = (
        (
            "CREATE OR REPLACE FUNCTION "
            "app_private.raise_ledger_immutable_violation()",
            "REVOKE ALL ON FUNCTION "
            "app_private.raise_ledger_immutable_violation() FROM PUBLIC",
            "CREATE TRIGGER trg_deny_role_event_mutation",
            "ALTER FUNCTION "
            "app_private.raise_ledger_immutable_violation() "
            "OWNER TO app_security_owner",
        ),
        (
            "CREATE OR REPLACE FUNCTION "
            "app_private.rebuild_effective_role_permissions()",
            "REVOKE ALL ON FUNCTION "
            "app_private.rebuild_effective_role_permissions() FROM PUBLIC",
            "ALTER FUNCTION "
            "app_private.rebuild_effective_role_permissions() "
            "OWNER TO app_security_owner",
        ),
        (
            "CREATE OR REPLACE FUNCTION "
            "app_private.trigger_rebuild_role_permissions()",
            "REVOKE ALL ON FUNCTION "
            "app_private.trigger_rebuild_role_permissions() FROM PUBLIC",
            "CREATE TRIGGER trg_auto_rebuild_role_perms",
            "ALTER FUNCTION "
            "app_private.trigger_rebuild_role_permissions() "
            "OWNER TO app_security_owner",
        ),
    )
    for sequence in ordered:
        positions = [upgrade.index(token) for token in sequence]
        assert positions == sorted(positions)


def test_0028_projection_reader_grant_runs_as_owner_after_rb1l7_prepare():
    upgrade = _rb1m2w_function("upgrade")
    create = upgrade.index(
        "CREATE TABLE public.effective_role_permissions"
    )
    transfer = upgrade.index(
        "ALTER TABLE public.effective_role_permissions "
        "OWNER TO app_security_owner"
    )
    rb1l7_prepare = upgrade.index(
        "_rb1l7_prepare_revision_schema_acl_state()"
    )
    reader_grant = upgrade.index(
        "_rb1m2w_execute_as_security_owner("
    )
    grant_sql = upgrade.index(
        "GRANT SELECT ON public.effective_role_permissions "
        "TO app_runtime, readonly_analytics"
    )
    assert create < transfer < rb1l7_prepare < reader_grant <= grant_sql
    assert (
        'op.execute("GRANT SELECT ON public.effective_role_permissions '
        'TO app_runtime, readonly_analytics;")'
        not in upgrade
    )


def test_0028_compile_replacement_and_upgrade_window_close_before_seed():
    upgrade = _rb1m2w_function("upgrade")
    compile_call = upgrade.index(
        "_rb1m2w_execute_as_security_owner("
    )
    compile_sql = upgrade.index(
        "CREATE OR REPLACE FUNCTION "
        "app_private.compile_member_permissions("
    )
    restore = upgrade.index(
        "_rb1m2w_restore_owner_context(bind, owner_state)"
    )
    seed = upgrade.index(
        "INSERT INTO public.role_permission_events"
    )
    finalize = upgrade.index(
        "_rb1l7_finalize_revision_schema_acl_state()"
    )
    assert compile_call <= compile_sql < restore < seed < finalize


def test_0028_downgrade_keeps_rb1l7_state_until_owner_ddl_finishes():
    downgrade = _rb1m2w_function("downgrade")
    assert downgrade.index("bind = _rb1l7_bind()") < downgrade.index(
        "_rb1m2w_preflight_downgrade(bind)"
    )
    assert downgrade.index("_rb1m2w_preflight_downgrade(bind)") < downgrade.index(
        "owner_state = _rb1m2w_prepare_owner_context(bind)"
    )
    compile_sql = downgrade.index(
        "CREATE OR REPLACE FUNCTION "
        "app_private.compile_member_permissions("
    )
    projection_drop = downgrade.index(
        '_rb1m2w_drop_owned_relation('
    )
    owner_restore = downgrade.index(
        "_rb1m2w_restore_owner_context(bind, owner_state)"
    )
    rb1l7_restore = downgrade.index(
        "_rb1l7_restore_revision_schema_acl_state()"
    )
    assert compile_sql < projection_drop < owner_restore < rb1l7_restore
    assert downgrade.count("_rb1l7_restore_revision_schema_acl_state()") == 1


def test_0028_downgrade_owner_objects_are_bounded_and_restrict_only():
    downgrade = _rb1m2w_function("downgrade")
    for signature in (
        "app_private.trigger_rebuild_role_permissions()",
        "app_private.rebuild_effective_role_permissions()",
        "app_private.raise_ledger_immutable_violation()",
    ):
        assert (
            "_rb1m2w_drop_owned_function(\n"
            f"        bind, '{signature}'\n"
            "    )"
            in downgrade
        )
    assert (
        '_rb1m2w_drop_owned_relation(\n'
        '        bind, "public.effective_role_permissions"\n'
        "    )"
        in downgrade
    )
    assert (
        "DROP TABLE IF EXISTS public.role_permission_events RESTRICT;"
        in downgrade
    )
    assert "DROP FUNCTION IF EXISTS app_private" not in downgrade
    assert (
        "DROP TABLE IF EXISTS public.effective_role_permissions"
        not in downgrade
    )
    assert "CASCADE" not in downgrade


def test_0028_owner_role_switch_resets_and_checks_identity():
    execute = _rb1m2w_function("_rb1m2w_execute_as_security_owner")
    for required in (
        'sa.text("SET LOCAL ROLE app_security_owner")',
        'sa.text("RESET ROLE")',
        "session_user_name",
        "current_user_name",
        "_rb1m2w_require_migration_owner(bind)",
    ):
        assert required in execute


def test_0028_complete_owner_context_preserves_least_privilege_boundaries():
    source = _rb1m2w_source()
    upgrade = _rb1m2w_function("upgrade")
    downgrade = _rb1m2w_function("downgrade")

    assert source.count("OWNER TO app_security_owner") == 4
    assert source.count("_rb1m2w_restore_owner_context(bind, owner_state)") == 2
    assert upgrade.count("_rb1l7_prepare_revision_schema_acl_state()") == 1
    assert upgrade.count("_rb1l7_finalize_revision_schema_acl_state()") == 1
    assert downgrade.count("_rb1l7_restore_revision_schema_acl_state()") == 1

    for forbidden in (
        "GRANT ALL ON SCHEMA",
        "ALTER SCHEMA app_private OWNER",
        "ALTER SCHEMA public OWNER",
        "GRANT CREATE ON SCHEMA app_private TO PUBLIC",
        "GRANT CREATE ON SCHEMA public TO PUBLIC",
        "SUPERUSER",
        "CREATEROLE",
        "BYPASSRLS",
        "DROP OWNED",
    ):
        assert forbidden not in source


def test_0028_direct_schema_acl_capture_excludes_implicit_defaults():
    public_check = _rb1m2w_function(
        "_rb1m2w_public_has_schema_create"
    )
    direct_rows = _rb1m2w_function(
        "_rb1m2w_direct_schema_acl_rows"
    )

    for helper in (public_check, direct_rows):
        assert "pg_catalog.aclexplode(" in helper
        assert "namespace.nspacl" in helper
        assert "acldefault(" not in helper
        assert "COALESCE(" not in helper


def test_0028_ephemeral_restore_tracks_create_only_and_allows_durable_usage():
    prepare = _rb1m2w_function("_rb1m2w_prepare_owner_context")
    restore = _rb1m2w_function("_rb1m2w_restore_owner_context")

    assert '"before_create"' in prepare
    assert 'state["before_create"]' in restore
    assert 'if row[2] == "CREATE"' in prepare
    assert 'if row[2] == "CREATE"' in restore
    assert 'state["before"]' not in restore
    assert "Ephemeral CREATE-only journal" in prepare
    assert "RB1L7's public-schema USAGE grant" in prepare
    assert "schema ACL restoration failed" in restore
    assert "temporary CREATE ACL restoration" in restore


def test_0028_downgrade_restores_exact_0027_compile_member_permissions_definition():
    import ast as _rb1m2w_r8_ast
    import hashlib as _rb1m2w_r8_hashlib

    source = _rb1m2w_source()
    tree = _rb1m2w_r8_ast.parse(source)
    downgrade_nodes = [
        node for node in tree.body
        if isinstance(node, _rb1m2w_r8_ast.FunctionDef)
        and node.name == "downgrade"
    ]
    assert len(downgrade_nodes) == 1

    restored_sql = []
    for node in _rb1m2w_r8_ast.walk(downgrade_nodes[0]):
        if not isinstance(node, _rb1m2w_r8_ast.Call):
            continue
        if not isinstance(node.func, _rb1m2w_r8_ast.Name):
            continue
        if node.func.id != "_rb1m2w_execute_as_security_owner":
            continue
        if len(node.args) != 2:
            continue
        sql_arg = node.args[1]
        if not isinstance(sql_arg, _rb1m2w_r8_ast.Constant):
            continue
        if not isinstance(sql_arg.value, str):
            continue
        if (
            "CREATE OR REPLACE FUNCTION "
            "app_private.compile_member_permissions"
            in sql_arg.value
        ):
            restored_sql.append(sql_arg.value)

    assert len(restored_sql) == 1
    restored = restored_sql[0]
    assert _rb1m2w_r8_hashlib.sha256(
        restored.encode("utf-8")
    ).hexdigest() == "077b62b64641a0cc3a4aca1347a7e7b3252af57cda44ff815d38b40c26ea5221"
    assert (
        "-- Derive the permission codes from active role assignments."
        in restored
    )
    assert (
        "-- This will be replaced by public.role_permission_events "
        "in Phase 7."
        in restored
    )
    assert "-- Return empty array if no permissions found (not NULL)" in restored



# RB1M2V_PHASE10_OWNER_CONTEXT_REGRESSION_START
def _rb1m2v_phase10_source(relative_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return (root / relative_path).read_text(encoding="utf-8")


def test_phase10_f71_is_validation_only_and_45df_retains_owner_context():
    f71 = _rb1m2v_phase10_source("alembic/versions/f71f231fb001_rbac_hardening_phase_10_partitioned_.py")
    assert "audit-ledger preservation checkpoint" in f71
    assert "def _validate_predecessor_audit_contract" in f71
    assert "RB1M2V_PHASE10_APP_PRIVATE_OWNER_CONTEXT_START" not in f71
    assert "DROP TABLE IF EXISTS public.branch_audit_log CASCADE" not in f71
    assert "CREATE TABLE public.branch_audit_log" not in f71
    assert "CREATE OR REPLACE FUNCTION app_private.raise_immutable_violation" not in f71
    assert "ALTER FUNCTION app_private.raise_immutable_violation" not in f71
    audit = _rb1m2v_phase10_source("alembic/versions/45df3b75ed74_rbac_hardening_phase_10_audit_functions.py")
    for required in ("RB1M2V_PHASE10_APP_PRIVATE_OWNER_CONTEXT_START","def _rb1m2v_prepare_app_private_owner_window","def _rb1m2v_restore_app_private_owner_window","def _rb1m2v_run_as_security_owner","def _rb1m2v_transfer_function_owner","def _rb1m2v_drop_function_if_exists","SET LOCAL ROLE app_security_owner","RESET ROLE"):
        assert required in audit
    for forbidden in ("GRANT CREATE ON SCHEMA app_private TO PUBLIC","GRANT CREATE ON SCHEMA app_private TO app_runtime","GRANT CREATE ON SCHEMA app_private TO migration_owner","DROP OWNED","session_replication_role"):
        assert forbidden not in audit


def test_f71_validator_covers_data_compatible_predecessor_contract():
    f71 = _rb1m2v_phase10_source("alembic/versions/f71f231fb001_rbac_hardening_phase_10_partitioned_.py")
    validator = f71[f71.index("def _validate_predecessor_audit_contract"):f71.index("def upgrade()")]
    assert validator.count("'[[:space:]]+'")==3
    assert r"\s" not in validator
    for required in ("to_regclass('public.branch_audit_log')","to_regclass('public.branch_audit_log_seq')","v_parent_kind <> 'p'","RANGE (created_at)","('reason', 'text', FALSE)","'event_hash'","'character varying(64)'","event_hash is null","previous_event_hash is not null","('audit_sequence', 'bigint', TRUE)","chk_reason_on_destructive","chk_prev_hash_chain","system.bootstrap","branch_audit_log_y2026_m05","branch_audit_log_y2026_m06","branch_audit_log_y2026_m07","branch_audit_log_y2026_m08","Extra future partitions are valid","raise_immutable_audit_violation()","trg_deny_audit_mutation","partition trigger clone contract is invalid","duplicate immutable function exists","org_advisory_lock_key(uuid)","append_audit_event(","ensure_future_partition(text,integer)","app_security_owner","security/cluster_role_bootstrap","pg_catalog.pg_roles","rolsuper","rolcreatedb","rolcreaterole","rolreplication","rolbypassrls","rolcanlogin","rolinherit","has_table_privilege","has_sequence_privilege","app_rls_executor","PUBLIC table privilege exists"):
        assert required in f71

    legacy = f71[f71.index("-- Legacy policy: USING only."):f71.index("-- Revision-0026 policy:")]
    for required in ("tenant_isolation_audit", "polqual IS NULL", "'org_id='", "'app.current_org_id'", "'current_setting'", "',true)'", "'::uuid'", "' or '", "legacy tenant policy is not fail-closed"):
        assert required in legacy
    for optional in ("nullif(", "pg_input_is_valid"):
        assert optional not in legacy.lower()
    assert legacy.count("'[[:space:]]+'")==1

    strict = f71[f71.index("-- Revision-0026 policy:"):f71.index("-- Parent immutable trigger")]
    for required in ("tenant_isolation_audit_log", "polqual IS NULL", "polwithcheck IS NULL", "using_text", "check_text", "strict tenant policy is not fail-closed"):
        assert required in strict
    assert strict.count("'app.current_org_id'") >= 2
    assert strict.count("'current_setting'") >= 2
    assert strict.count("',false)'") >= 2
    assert strict.count("'::uuid'") >= 2
    assert strict.count("'org_id='") >= 2
    assert strict.count("' or '") >= 2
    assert strict.count("'[[:space:]]+'")==2


def test_f71_upgrade_and_downgrade_call_only_the_validator():
    import ast, re
    f71 = _rb1m2v_phase10_source("alembic/versions/f71f231fb001_rbac_hardening_phase_10_partitioned_.py")
    tree=ast.parse(f71); functions={node.name:node for node in tree.body if isinstance(node,ast.FunctionDef)}
    for name in ("upgrade","downgrade"):
        calls=[node.value for node in functions[name].body if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)]
        assert len(calls)==1 and isinstance(calls[0].func,ast.Name) and calls[0].func.id=="_validate_predecessor_audit_contract" and not calls[0].args and not calls[0].keywords
    execute_calls=[node for node in ast.walk(functions["_validate_predecessor_audit_contract"]) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="execute"]
    assert len(execute_calls)==1
    sql=ast.literal_eval(execute_calls[0].args[0]); normalized=" ".join(sql.lower().split())
    forbidden=r"\b(create|alter|drop|truncate|grant|revoke)\s+(table|role|function|trigger|policy|index|sequence|schema|view)\b"
    assert re.search(forbidden,normalized) is None
    assert "insert into public.branch_audit_log" not in normalized
    assert "update public.branch_audit_log set" not in normalized
    assert "delete from public.branch_audit_log" not in normalized


def test_45df_owner_transfer_and_predecessor_restoration_are_ordered():
    audit=_rb1m2v_phase10_source("alembic/versions/45df3b75ed74_rbac_hardening_phase_10_audit_functions.py")
    upgrade=audit[audit.index("def upgrade() -> None:"):audit.index("def downgrade() -> None:")]
    assert upgrade.count("_rb1m2v_prepare_app_private_owner_window(bind)")==1
    assert upgrade.count("_rb1m2v_transfer_function_owner(")==2
    assert upgrade.count("_rb1m2v_run_as_security_owner(")==2
    assert upgrade.count("_rb1m2v_restore_app_private_owner_window(bind, owner_state)")==1
    downgrade=audit[audit.index("def downgrade() -> None:"):]
    assert "_rb1m2v_prepare_app_private_owner_window(bind)" in downgrade
    assert "CREATE OR REPLACE FUNCTION app_private.org_advisory_lock_key(p_org_id UUID)" in downgrade
    assert "_rb1m2v_transfer_function_owner(" in downgrade
    assert "_rb1m2v_run_as_security_owner(" in downgrade
    assert "_rb1m2v_restore_app_private_owner_window(bind, owner_state)" in downgrade
# RB1M2V_PHASE10_OWNER_CONTEXT_REGRESSION_END


# RB1M2V_R3_45DF_PREDECESSOR_FUNCTION_RESTORATION_START
def test_45df_downgrade_restores_predecessor_org_advisory_lock_contract():
    text = _rb1m2v_phase10_source(
        "alembic/versions/45df3b75ed74_rbac_hardening_phase_10_audit_functions.py"
    )
    downgrade = text[text.index("def downgrade() -> None:"):]

    predecessor_create = (
        "CREATE OR REPLACE FUNCTION "
        "app_private.org_advisory_lock_key(p_org_id UUID)"
    )
    assert predecessor_create in downgrade
    assert "RETURNS BIGINT" in downgrade
    assert "STRICT" in downgrade
    assert "IMMUTABLE" in downgrade
    assert "PARALLEL SAFE" in downgrade
    assert "SECURITY DEFINER" in downgrade
    assert "SET search_path = pg_catalog" in downgrade
    assert (
        "RETURN (('x' || substr(md5(p_org_id::text), 1, 16)))"
        "::bit(64)::bigint;"
        in downgrade
    )

    assert (
        downgrade.index("_rb1m2v_prepare_app_private_owner_window(bind)")
        < downgrade.index(
            '"app_private.append_audit_event("'
        )
        < downgrade.index(
            '"app_private.org_advisory_lock_key(uuid)"'
        )
        < downgrade.index(predecessor_create)
        < downgrade.index("_rb1m2v_transfer_function_owner(")
        < downgrade.index("_rb1m2v_run_as_security_owner(")
        < downgrade.index(
            "_rb1m2v_restore_app_private_owner_window(bind, owner_state)"
        )
    )

    assert (
        "REVOKE ALL ON FUNCTION "
        '"\n            "app_private.org_advisory_lock_key(uuid) FROM PUBLIC"'
        in downgrade
    )
    assert (
        "GRANT EXECUTE ON FUNCTION "
        '"\n            "app_private.org_advisory_lock_key(uuid) TO audit_writer"'
        in downgrade
    )
    assert (
        '_rb1m2v_drop_function_if_exists(\n'
        '        bind,\n'
        '        "app_private.org_advisory_lock_key(uuid)",\n'
        '    )'
        in downgrade
    )
# RB1M2V_R3_45DF_PREDECESSOR_FUNCTION_RESTORATION_END
