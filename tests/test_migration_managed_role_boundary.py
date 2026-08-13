from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "alembic/versions/0020_contacts_hardened.py",
    ROOT / "alembic/versions/0022_rbac_phase1_roles_extensions.py",
    ROOT / "alembic/versions/00f277c748ea_add_hyperscale_branch_name_and_address_.py",
    ROOT / "alembic/versions/f71f231fb001_rbac_hardening_phase_10_partitioned_.py",
)
MANAGED_ROLES = (
    "app_rls_executor", "app_runtime", "app_security_owner", "app_user",
    "audit_writer", "branch_admin", "branch_viewer", "migration_owner",
    "ops_support", "readonly_analytics",
)
MUTATIONS = (
    re.compile(r"\bCREATE\s+ROLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+ROLE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+ROLE\b", re.IGNORECASE),
    re.compile(r"\bCOMMENT\s+ON\s+ROLE\b", re.IGNORECASE),
    re.compile(r"\bGRANT\s+[A-Za-z_][A-Za-z0-9_]*\s+TO\s+[A-Za-z_][A-Za-z0-9_]*", re.IGNORECASE),
    re.compile(r"\bREVOKE\s+[A-Za-z_][A-Za-z0-9_]*\s+FROM\s+[A-Za-z_][A-Za-z0-9_]*", re.IGNORECASE),
)
GUIDANCE = "security/cluster_role_bootstrap"

def _joined_text(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{EXPR}")
    return "".join(parts)

def _strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            values.append(_joined_text(node))
    return values

def test_no_executable_managed_role_mutation_remains() -> None:
    failures: list[str] = []
    for path in MIGRATIONS:
        for value in _strings(path):
            if not any(role in value for role in MANAGED_ROLES):
                continue
            for pattern in MUTATIONS:
                if pattern.search(value):
                    failures.append(f"{path.name}: {pattern.pattern}: {value.strip()}")
    assert not failures, "\n".join(failures)

def test_all_migrations_validate_required_external_roles() -> None:
    required_roles = {
        "0020_contacts_hardened.py": {"app_rls_executor", "app_user"},
        "0022_rbac_phase1_roles_extensions.py": {"app_security_owner", "app_runtime", "audit_writer", "readonly_analytics"},
        "00f277c748ea_add_hyperscale_branch_name_and_address_.py": {"branch_admin", "branch_viewer", "ops_support"},
        "f71f231fb001_rbac_hardening_phase_10_partitioned_.py": {"app_security_owner", "audit_writer"},
    }
    for path in MIGRATIONS:
        source = path.read_text(encoding="utf-8")
        for required in (GUIDANCE, "pg_catalog.pg_roles", "rolsuper", "rolbypassrls", "rolcanlogin", "rolinherit", "RAISE EXCEPTION"):
            assert required in source, f"{path.name}: missing {required}"
        for role in required_roles[path.name]:
            assert role in source, f"{path.name}: missing {role}"

def test_app_runtime_settings_are_validated_read_only() -> None:
    source = (ROOT / "alembic/versions/0022_rbac_phase1_roles_extensions.py").read_text(encoding="utf-8")
    for required in ("statement_timeout=5s", "lock_timeout=2s", "row_security=on", "role_settings.v1.json"):
        assert required in source
    assert "ALTER ROLE app_runtime SET" not in source

def test_downgrades_do_not_delete_managed_roles() -> None:
    for path in MIGRATIONS:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        downgrade = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "downgrade"), None)
        if downgrade is None:
            continue
        for node in ast.walk(downgrade):
            value = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else _joined_text(node) if isinstance(node, ast.JoinedStr) else None
            if value is not None:
                assert not re.search(r"\bDROP\s+ROLE\b", value, re.IGNORECASE), f"{path.name}: managed role deletion remains"

def test_legacy_role_creation_helpers_are_removed() -> None:
    for path in MIGRATIONS:
        source = path.read_text(encoding="utf-8")
        assert "_role_exists(" not in source
        managed_strings = "\n".join(value for value in _strings(path) if any(role in value for role in MANAGED_ROLES))
        assert "WHEN duplicate_object THEN NULL" not in managed_strings

# RB1K_OWNER_BOUNDARY_STATIC_REGRESSIONS_BEGIN

def _rb1k_0020_nodes():
    import ast
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0020_contacts_hardened.py"
    )
    source = migration_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(migration_path))

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"upgrade", "downgrade"}
    }

    assert set(functions) == {"upgrade", "downgrade"}
    return source, functions["upgrade"], functions["downgrade"]


def _rb1k_execute_sql(statement):
    import ast

    if not isinstance(statement, ast.Expr):
        return None

    call = statement.value

    if not isinstance(call, ast.Call) or not call.args:
        return None

    if not ast.unparse(call.func).endswith("execute"):
        return None

    argument = call.args[0]

    if (
        not isinstance(argument, ast.Constant)
        or not isinstance(argument.value, str)
    ):
        return None

    return argument.value.strip()


def _rb1k_expected_direct_transfers():
    return [
        (
            "ALTER TABLE public.branch_contacts "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER TABLE public.branch_contacts_audit "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER TABLE public.branch_contacts_audit_default "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.prevent_soft_delete_resurrection() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.prevent_audit_modification() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION app_private.update_timestamp() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.log_branch_contact_changes() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.process_primary_contact_batch(UUID[]) "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.ensure_primary_contact_insert() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.ensure_primary_contact_update() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.ensure_primary_contact_delete() "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER TABLE app_private.partition_metadata "
            "OWNER TO app_rls_executor;"
        ),
        (
            "ALTER FUNCTION "
            "app_private.create_branch_contacts_audit_partition(DATE) "
            "OWNER TO app_rls_executor;"
        ),
    ]


def test_0020_upgrade_defers_direct_owner_transfers_to_final_boundary():
    import re

    source, upgrade, _ = _rb1k_0020_nodes()
    owner_pattern = re.compile(
        r"^\s*ALTER\s+(?:TABLE|FUNCTION)\s+.+?"
        r"\s+OWNER\s+TO\s+app_rls_executor;\s*$",
        re.IGNORECASE | re.DOTALL,
    )

    sql_statements = [
        sql
        for statement in upgrade.body
        if (sql := _rb1k_execute_sql(statement)) is not None
    ]
    transfers = [
        sql
        for sql in sql_statements
        if owner_pattern.fullmatch(sql)
    ]
    expected = _rb1k_expected_direct_transfers()

    temporary_grant = (
        "GRANT CREATE ON SCHEMA app_private "
        "TO app_rls_executor;"
    )
    temporary_revoke = (
        "REVOKE CREATE ON SCHEMA app_private "
        "FROM app_rls_executor;"
    )
    expected_tail = (
        expected[:3]
        + [temporary_grant]
        + expected[3:]
        + [temporary_revoke]
    )

    assert transfers == expected
    assert sql_statements[-len(expected_tail):] == expected_tail
    assert sql_statements.count(temporary_grant) == 1
    assert sql_statements.count(temporary_revoke) == 1
    assert source.count("OWNER TO app_rls_executor") == 14
    assert source.count(
        "EXECUTE format('ALTER TABLE public.%I "
        "OWNER TO app_rls_executor', partition_name);"
    ) == 1


def test_0020_partition_owner_transfer_remains_after_partition_hardening():
    source, _, _ = _rb1k_0020_nodes()

    function_start = source.index(
        "CREATE FUNCTION "
        "app_private.create_branch_contacts_audit_partition"
    )
    function_end = source.index(
        "$$ LANGUAGE plpgsql;",
        function_start,
    )
    function_sql = source[function_start:function_end]

    ordered_fragments = [
        "ALTER COLUMN changed_fields SET COMPRESSION lz4",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "GRANT SELECT, INSERT",
        "OWNER TO app_rls_executor",
        "INSERT INTO app_private.partition_metadata",
    ]
    positions = [
        function_sql.index(fragment)
        for fragment in ordered_fragments
    ]

    assert positions == sorted(positions)


def test_0020_downgrade_bounds_owner_context_before_type_cleanup():
    _, _, downgrade = _rb1k_0020_nodes()

    sql_statements = [
        sql
        for statement in downgrade.body
        if (sql := _rb1k_execute_sql(statement)) is not None
    ]

    set_sql = "SET LOCAL ROLE app_rls_executor;"
    reset_sql = "RESET ROLE;"
    partition_drop = "DROP TABLE app_private.partition_metadata RESTRICT;"

    assert sql_statements.count(set_sql) == 1
    assert sql_statements.count(reset_sql) == 1

    set_index = sql_statements.index(set_sql)
    reset_index = sql_statements.index(reset_sql)
    partition_drop_index = sql_statements.index(partition_drop)
    first_type_index = next(
        index
        for index, sql in enumerate(sql_statements)
        if sql.startswith("DROP TYPE")
    )

    assert set_index == 0
    assert partition_drop_index < reset_index < first_type_index

    owner_cleanup = sql_statements[
        set_index + 1 : reset_index
    ]
    assert owner_cleanup
    assert all(
        sql.startswith(
            ("DROP TRIGGER", "DROP POLICY", "DROP FUNCTION", "DROP TABLE")
        )
        for sql in owner_cleanup
    )
    assert all(
        sql.startswith("DROP TYPE")
        for sql in sql_statements[reset_index + 1 :]
    )



# RB1L4_DEFAULT_PARTITION_STATIC_REGRESSIONS

def test_0020_default_partition_has_explicit_security_parity():
    source, upgrade, _ = _rb1k_0020_nodes()

    sql_statements = [
        sql
        for statement in upgrade.body
        if (sql := _rb1k_execute_sql(statement)) is not None
    ]

    expected_security = [
        (
            "ALTER TABLE public.branch_contacts_audit_default "
            "ALTER COLUMN changed_fields SET COMPRESSION lz4;"
        ),
        (
            "ALTER TABLE public.branch_contacts_audit_default "
            "ENABLE ROW LEVEL SECURITY;"
        ),
        (
            "ALTER TABLE public.branch_contacts_audit_default "
            "FORCE ROW LEVEL SECURITY;"
        ),
        (
            "GRANT SELECT, INSERT ON "
            "public.branch_contacts_audit_default TO app_user;"
        ),
    ]
    default_owner = (
        "ALTER TABLE public.branch_contacts_audit_default "
        "OWNER TO app_rls_executor;"
    )
    parent_owner = (
        "ALTER TABLE public.branch_contacts_audit "
        "OWNER TO app_rls_executor;"
    )
    temporary_private_grant = (
        "GRANT CREATE ON SCHEMA app_private "
        "TO app_rls_executor;"
    )

    for sql in expected_security:
        assert sql_statements.count(sql) == 1

    security_positions = [
        sql_statements.index(sql)
        for sql in expected_security
    ]

    assert security_positions == sorted(security_positions)
    assert security_positions[-1] < sql_statements.index(default_owner)
    assert (
        sql_statements.index(parent_owner)
        < sql_statements.index(default_owner)
        < sql_statements.index(temporary_private_grant)
    )

    expected_transfers = _rb1k_expected_direct_transfers()
    assert expected_transfers[:3] == [
        (
            "ALTER TABLE public.branch_contacts "
            "OWNER TO app_rls_executor;"
        ),
        parent_owner,
        default_owner,
    ]
    assert len(expected_transfers) == 13
    assert source.count("OWNER TO app_rls_executor") == 14


def test_0020_default_partition_adds_no_policy_trigger_index_or_broad_grant():
    import ast

    source, _, _ = _rb1k_0020_nodes()
    tree = ast.parse(source)

    sql_strings = [
        node.value.strip()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    ]

    default_name = "BRANCH_CONTACTS_AUDIT_DEFAULT"
    matching = [
        sql
        for sql in sql_strings
        if default_name in sql.upper()
    ]

    grants = [
        sql
        for sql in matching
        if sql.upper().startswith("GRANT ")
    ]

    assert grants == [
        (
            "GRANT SELECT, INSERT ON "
            "public.branch_contacts_audit_default TO app_user;"
        )
    ]

    assert not any(
        "CREATE POLICY" in sql.upper()
        for sql in matching
    )
    assert not any(
        "CREATE TRIGGER" in sql.upper()
        for sql in matching
    )
    assert not any(
        "CREATE INDEX" in sql.upper()
        or "CREATE UNIQUE INDEX" in sql.upper()
        for sql in matching
    )
    assert not any(
        privilege in grants[0].upper()
        for privilege in (
            " UPDATE",
            " DELETE",
            " TRUNCATE",
            " REFERENCES",
            " TRIGGER",
        )
    )

# RB1K_OWNER_BOUNDARY_STATIC_REGRESSIONS_END

