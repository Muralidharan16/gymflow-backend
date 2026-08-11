from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
A1 = VERSIONS / "a1b2c3d4e5f6_rbac_hardening_phase_15_to_18.py"
M361 = VERSIONS / "361c32e72e93_add_geo_fields_to_branches.py"
M0023 = VERSIONS / "0023_rbac_p2_ref_tables.py"
M0025 = VERSIONS / "0025_rbac_p4_bsr_expand.py"
M0027 = VERSIONS / "0027_rbac_p6_perm_snapshots.py"

APP_SECURE_FILES = {
    "0022_rbac_phase1_roles_extensions.py",
    "0025_rbac_p4_bsr_expand.py",
    "0027_rbac_p6_perm_snapshots.py",
    "0029_rbac_p8_contract.py",
    "6f708192a3b4_address_runtime_privilege_boundary.py",
    A1.name,
}

ACTIVE_VIEW = "app_secure.v_active_branch_staff_roles"
EFFECTIVE_VIEW = "app_secure.v_effective_member_permissions"
CANONICAL_VIEW_COMMENT = (
    "Tenant-safe security-invoker view of canonical branch staff roles."
)
CANONICAL_EFFECTIVE_VIEW_COMMENT = (
    "'Security-barrier, security-invoker view: ' "
    "'non-stale, non-expired permission snapshots.'"
)

GEO_COLUMNS = {
    "geo_country_id": ("smallint", "SmallInteger"),
    "geo_subdivision_id": ("bigint", "BigInteger"),
    "geo_city_id": ("bigint", "BigInteger"),
    "geo_postal_code_id": ("bigint", "BigInteger"),
}

GEO_FOREIGN_KEYS = (
    (
        "org_branches_geo_country_id_fkey",
        ("geo_country_id",),
        "countries",
        ("id",),
    ),
    (
        "org_branches_geo_subdivision_id_fkey",
        ("geo_subdivision_id",),
        "subdivisions",
        ("id",),
    ),
    (
        "fk_org_branch_subdivision_country",
        ("geo_subdivision_id", "geo_country_id"),
        "subdivisions",
        ("id", "country_id"),
    ),
    (
        "org_branches_geo_city_id_fkey",
        ("geo_city_id",),
        "cities",
        ("id",),
    ),
    (
        "fk_org_branch_city_country",
        ("geo_city_id", "geo_country_id"),
        "cities",
        ("id", "country_id"),
    ),
    (
        "org_branches_geo_postal_code_id_fkey",
        ("geo_postal_code_id",),
        "postal_codes",
        ("id",),
    ),
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in _tree(path).body
        if isinstance(node, ast.FunctionDef)
    }


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    node = _functions(path)[name]
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _string_constants(node: ast.AST) -> list[str]:
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _literal_text(path: Path) -> str:
    return _normalized("\n".join(_string_constants(_tree(path))))


def _function_literal_text(path: Path, name: str) -> str:
    return _normalized(
        "\n".join(_string_constants(_functions(path)[name]))
    )


def _module_string_assignment(path: Path, name: str) -> str:
    for node in _tree(path).body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            value = node.value
        if value is not None:
            observed = ast.literal_eval(value)
            assert isinstance(observed, str)
            return observed
    raise AssertionError(f"Missing module string assignment {name}")


def _module_literal_assignment(path: Path, name: str):
    for node in _tree(path).body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            value = node.value
        if value is not None:
            return ast.literal_eval(value)
    raise AssertionError(f"Missing module literal assignment {name}")


def _direct_op_calls(path: Path, name: str) -> list[ast.Call]:
    calls = []
    for item in ast.walk(_functions(path)[name]):
        if not isinstance(item, ast.Call):
            continue
        if not isinstance(item.func, ast.Attribute):
            continue
        if not isinstance(item.func.value, ast.Name):
            continue
        if item.func.value.id != "op" or item.func.attr == "get_bind":
            continue
        calls.append(item)
    return sorted(calls, key=lambda item: item.lineno)


def _keyword(call: ast.Call, name: str) -> ast.expr:
    for item in call.keywords:
        if item.arg == name:
            return item.value
    raise AssertionError(f"Missing keyword {name}")


def _attribute_name(node: ast.expr) -> str:
    assert isinstance(node, ast.Call)
    assert isinstance(node.func, ast.Attribute)
    assert isinstance(node.func.value, ast.Name)
    assert node.func.value.id == "sa"
    return node.func.attr


def _direct_module_calls(path: Path, name: str) -> list[tuple[int, str]]:
    functions = _functions(path)
    calls = []
    for item in ast.walk(functions[name]):
        if not isinstance(item, ast.Call):
            continue
        if not isinstance(item.func, ast.Name):
            continue
        if item.func.id not in functions:
            continue
        calls.append((item.lineno, item.func.id))
    return sorted(calls)


def _reachable_names(path: Path, root: str) -> set[str]:
    functions = _functions(path)
    result: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in result:
            continue
        result.add(name)
        pending.extend(
            called
            for _, called in _direct_module_calls(path, name)
            if called in functions
        )
    return result


def _reachable_source(path: Path, root: str) -> str:
    return "\n".join(
        _function_source(path, name)
        for name in sorted(_reachable_names(path, root))
    )


def _call_lines(path: Path, root: str, names: set[str]) -> list[int]:
    return [
        line
        for line, name in _direct_module_calls(path, root)
        if name in names
    ]


def _op_mutation_lines(
    path: Path,
    root: str,
    *,
    touching: tuple[str, ...] = (),
) -> list[int]:
    source = _source(path)
    result = []
    for item in ast.walk(_functions(path)[root]):
        if not isinstance(item, ast.Call):
            continue
        if not isinstance(item.func, ast.Attribute):
            continue
        if not isinstance(item.func.value, ast.Name):
            continue
        if item.func.value.id != "op" or item.func.attr == "get_bind":
            continue
        segment = ast.get_source_segment(source, item) or ""
        if touching and not any(token in segment for token in touching):
            continue
        result.append(item.lineno)
    return sorted(result)


def _dependent_staff_role_type_change_lines(
    path: Path, root: str
) -> list[int]:
    result = []
    for item in ast.walk(_functions(path)[root]):
        if not isinstance(item, ast.Call):
            continue
        if not isinstance(item.func, ast.Attribute):
            continue
        if not isinstance(item.func.value, ast.Name):
            continue
        if item.func.value.id != "op" or item.func.attr != "alter_column":
            continue
        if len(item.args) < 2:
            continue
        relation = item.args[0]
        column = item.args[1]
        if not (
            isinstance(relation, ast.Constant)
            and relation.value == "branch_staff_roles"
            and isinstance(column, ast.Constant)
            and column.value in {"role_id", "scope_type_id"}
        ):
            continue
        result.append(item.lineno)
    return sorted(result)


def _functions_with_sql(path: Path, pattern: str) -> set[str]:
    expression = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    return {
        name
        for name in _functions(path)
        if expression.search(_normalized(_function_source(path, name)))
    }


def _app_secure_ddl_categories(path: Path) -> set[str]:
    strings = _string_constants(_tree(path))
    text = "\n".join(_normalized(value) for value in strings)
    patterns = {
        "create_schema": r"\bCREATE SCHEMA app_secure\b",
        "revoke_schema": r"\bREVOKE\b.+?\bON SCHEMA app_secure\b",
        "grant_schema": r"\bGRANT\b.+?\bON SCHEMA app_secure\b",
        "default_privileges": (
            r"\bALTER DEFAULT PRIVILEGES IN SCHEMA app_secure\b"
        ),
        "comment_schema": r"\bCOMMENT ON SCHEMA app_secure\b",
        "drop_schema": r"\bDROP SCHEMA(?: IF EXISTS)? app_secure\b",
        "create_view": (
            r"\bCREATE(?: OR REPLACE)? VIEW app_secure\.[a-z0-9_]+\b"
        ),
        "drop_view": (
            r"\bDROP VIEW(?: IF EXISTS)? app_secure\.[a-z0-9_]+\b"
        ),
        "revoke_view": (
            r"\bREVOKE\b.+?\bON(?: TABLE)? "
            r"app_secure\.[a-z0-9_]+\b"
        ),
        "grant_view": (
            r"\bGRANT\b.+?\bON(?: TABLE)? "
            r"app_secure\.[a-z0-9_]+\b"
        ),
        "comment_view": (
            r"\bCOMMENT ON VIEW app_secure\.[a-z0-9_]+\b"
        ),
    }
    return {
        category
        for category, pattern in patterns.items()
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    }


def _assert_read_only_preflight(path: Path, name: str) -> None:
    functions = _functions(path)
    reachable = _reachable_names(path, name)
    for helper_name in reachable:
        node = functions[helper_name]
        for item in ast.walk(node):
            if not isinstance(item, ast.Call):
                continue
            if not isinstance(item.func, ast.Attribute):
                continue
            if isinstance(item.func.value, ast.Name):
                assert item.func.value.id != "op"
            if item.func.attr not in {"execute", "exec_driver_sql"}:
                continue
            for value in _string_constants(item):
                normalized = _normalized(value)
                assert not re.match(
                    r"^(?:CREATE|ALTER|DROP|GRANT|REVOKE|SET|RESET|"
                    r"INSERT|UPDATE|DELETE|TRUNCATE)\b",
                    normalized,
                    re.IGNORECASE,
                ), (helper_name, normalized)


def _assert_simple_active_view_contract(text: str) -> None:
    normalized = _normalized(text)
    assert re.search(
        rf"CREATE(?: OR REPLACE)? VIEW {re.escape(ACTIVE_VIEW)}\b",
        normalized,
        re.IGNORECASE,
    )
    assert "security_barrier = true" in normalized
    assert "security_invoker = true" in normalized
    assert (
        "SELECT * FROM public.branch_staff_roles "
        "WHERE deleted_at IS NULL AND revoked_at IS NULL"
        in normalized
    )
    assert "JOIN public.staff_roles" not in normalized
    assert "JOIN public.scope_types" not in normalized
    _assert_active_view_acl_and_comment(normalized)


def _assert_joined_active_view_contract(text: str) -> None:
    normalized = _normalized(text)
    assert re.search(
        rf"CREATE(?: OR REPLACE)? VIEW {re.escape(ACTIVE_VIEW)}\b",
        normalized,
        re.IGNORECASE,
    )
    assert "security_barrier = true" in normalized
    assert "security_invoker = true" in normalized
    projection = (
        "SELECT bsr.id, bsr.org_id, bsr.branch_id, "
        "bsr.organization_member_id, bsr.role_id, sr.code AS role_code, "
        "sr.hierarchy_level, bsr.scope_type_id, st.code AS scope_code, "
        "bsr.assignment_source, bsr.assigned_by, bsr.assigned_at, "
        "bsr.effective_from, bsr.effective_to, om.user_id AS user_id, "
        "sr.code AS role_legacy, bsr.created_at"
    )
    assert projection in normalized
    assert "FROM public.branch_staff_roles AS bsr" in normalized
    assert "JOIN public.organization_members AS om" in normalized
    assert "ON om.id = bsr.organization_member_id" in normalized
    assert "AND om.org_id = bsr.org_id" in normalized
    assert "JOIN public.staff_roles AS sr ON sr.id = bsr.role_id" in normalized
    assert "JOIN public.scope_types AS st ON st.id = bsr.scope_type_id" in normalized
    assert "WHERE bsr.deleted_at IS NULL AND bsr.revoked_at IS NULL" in normalized
    _assert_active_view_acl_and_comment(normalized)


def _assert_active_view_acl_and_comment(normalized: str) -> None:
    assert f"REVOKE ALL ON {ACTIVE_VIEW} FROM PUBLIC" in normalized
    assert (
        f"GRANT SELECT ON {ACTIVE_VIEW} TO app_runtime, readonly_analytics"
        in normalized
    )
    assert CANONICAL_VIEW_COMMENT in normalized


def _assert_effective_view_contract(text: str) -> None:
    normalized = _normalized(text)
    assert re.search(
        rf"CREATE(?: OR REPLACE)? VIEW {re.escape(EFFECTIVE_VIEW)}\b",
        normalized,
        re.IGNORECASE,
    )
    assert "security_barrier = true" in normalized
    assert "security_invoker = true" in normalized
    projection = (
        "SELECT mps.id, mps.org_id, mps.organization_member_id, "
        "mps.scope_type_id, st.code AS scope_code, mps.branch_id, "
        "mps.compiled_permissions, mps.snapshot_version, mps.is_stale, "
        "mps.expires_at, mps.created_at, mps.updated_at"
    )
    assert projection in normalized
    assert "FROM public.member_permission_snapshots AS mps" in normalized
    assert "JOIN public.scope_types AS st ON st.id = mps.scope_type_id" in normalized
    assert (
        "WHERE mps.is_stale = FALSE "
        "AND mps.expires_at > clock_timestamp()"
        in normalized
    )
    assert f"REVOKE ALL ON TABLE {EFFECTIVE_VIEW} FROM PUBLIC" in normalized
    assert (
        f"GRANT SELECT ON TABLE {EFFECTIVE_VIEW} "
        "TO app_runtime, readonly_analytics"
        in normalized
    )
    assert CANONICAL_EFFECTIVE_VIEW_COMMENT in normalized


def test_complete_88_revision_app_secure_file_inventory_is_closed() -> None:
    migrations = sorted(VERSIONS.glob("*.py"))
    assert len(migrations) == 88
    actual = {
        path.name
        for path in migrations
        if "app_secure" in _source(path).lower()
    }
    assert actual == APP_SECURE_FILES


def test_complete_app_secure_ddl_category_allowlist_is_exact() -> None:
    view_contract = {
        "create_view",
        "drop_view",
        "revoke_view",
        "grant_view",
        "comment_view",
    }
    expected = {
        "0022_rbac_phase1_roles_extensions.py": {
            "create_schema",
            "revoke_schema",
            "grant_schema",
            "default_privileges",
            "comment_schema",
            "drop_schema",
        },
        "0025_rbac_p4_bsr_expand.py": view_contract,
        "0027_rbac_p6_perm_snapshots.py": view_contract,
        "0029_rbac_p8_contract.py": view_contract,
        "6f708192a3b4_address_runtime_privilege_boundary.py": {
            "grant_schema",
            "revoke_schema",
        },
        A1.name: view_contract,
    }
    actual = {
        name: _app_secure_ddl_categories(VERSIONS / name)
        for name in APP_SECURE_FILES
    }
    assert actual == expected


def test_a1_owner_context_helpers_are_revision_local_and_preflight_first() -> None:
    source = _source(A1)
    assert source.count("A1B2C3D4E5F6_OWNER_CONTEXT_HELPERS_START") == 1
    assert source.count("A1B2C3D4E5F6_OWNER_CONTEXT_HELPERS_END") == 1
    assert "from alembic.versions" not in source
    functions = _functions(A1)
    assert "_a1_preflight" in functions
    preflight = _reachable_source(A1, "_a1_preflight")
    for token in (
        "session_user",
        "current_user",
        "migration_owner",
        "app_security_owner",
        "pg_has_role",
        "app_secure",
        "nspowner",
        "relowner",
        "tenant_isolation_staff_roles",
        "relrowsecurity",
        "relforcerowsecurity",
    ):
        assert token in preflight
    _assert_read_only_preflight(A1, "_a1_preflight")

    for direction in ("upgrade", "downgrade"):
        calls = [
            (line, name)
            for line, name in _direct_module_calls(A1, direction)
            if name != "_a1_bind"
        ]
        assert calls
        assert calls[0][1] == "_a1_preflight"
        assert sum(name == "_a1_preflight" for _, name in calls) == 1

    upgrade = _normalized(_function_source(A1, "upgrade"))
    downgrade = _normalized(_function_source(A1, "downgrade"))
    assert 'expected_view_generation="predecessor"' in upgrade
    assert 'expected_function_generation="predecessor"' in upgrade
    assert 'expected_view_generation="forward"' in downgrade
    assert 'expected_function_generation="forward"' in downgrade


def test_a1_owner_runner_is_bounded_set_local_and_success_only_reset() -> None:
    runner = _function_source(A1, "_a1_run_as_security_owner")
    normalized = _normalized(runner)
    assert "_a1_require_migration_owner(bind)" in normalized
    assert "SET LOCAL ROLE app_security_owner" in normalized
    assert "RESET ROLE" in normalized
    assert normalized.index("SET LOCAL ROLE app_security_owner") < normalized.index(
        "RESET ROLE"
    )
    assert normalized.index("for statement in statements") < normalized.index(
        "RESET ROLE"
    )
    assert "session_user_name" in runner
    assert "current_user_name" in runner
    assert "rollback clears the LOCAL role" in runner
    assert not any(
        isinstance(node, ast.Try)
        for node in ast.walk(_functions(A1)["_a1_run_as_security_owner"])
    )
    assert "finally:" not in runner


def test_a1_has_no_schema_fallback_or_unbounded_protected_drop() -> None:
    source = _source(A1)
    assert not re.search(
        r"CREATE\s+SCHEMA(?:\s+IF\s+NOT\s+EXISTS)?\s+app_secure",
        source,
        re.IGNORECASE,
    )
    assert not re.search(
        r"DROP\s+VIEW(?:\s+IF\s+EXISTS)?\s+"
        r"app_secure\.v_active_branch_staff_roles\s+CASCADE",
        source,
        re.IGNORECASE,
    )
    replace = _normalized(_function_source(A1, "_a1_replace_active_view"))
    assert f"DROP VIEW {ACTIVE_VIEW} RESTRICT" in replace
    assert "_a1_run_as_security_owner" in replace


def test_a1_view_contract_is_direction_specific_and_acl_hardened() -> None:
    replacement_literals = _function_literal_text(
        A1, "_a1_replace_active_view"
    )
    common_contract = f"{replacement_literals} {CANONICAL_VIEW_COMMENT}"
    forward = _module_string_assignment(A1, "_A1_FORWARD_VIEW_SQL")
    predecessor = _module_string_assignment(
        A1, "_A1_PREDECESSOR_VIEW_SQL"
    )
    _assert_simple_active_view_contract(f"{forward} {common_contract}")
    _assert_joined_active_view_contract(f"{predecessor} {common_contract}")
    replace = _normalized(_function_source(A1, "_a1_replace_active_view"))
    assert "_a1_run_as_security_owner" in replace
    assert "_a1_verify_view_contract" in replace
    upgrade = _normalized(_function_source(A1, "upgrade"))
    downgrade = _normalized(_function_source(A1, "downgrade"))
    assert '_a1_replace_active_view(bind, "forward")' in upgrade
    assert '_a1_replace_active_view(bind, "predecessor")' in downgrade


def test_a1_partition_function_uses_exact_bounded_acl_window() -> None:
    source = _source(A1)
    prepare = _normalized(
        _function_source(A1, "_a1_prepare_app_private_owner_window")
    )
    restore = _normalized(
        _function_source(A1, "_a1_restore_app_private_owner_window")
    )
    replace = _normalized(_function_source(A1, "_a1_replace_partition_function"))
    replace_literals = _function_literal_text(
        A1, "_a1_replace_partition_function"
    )
    assert "pg_catalog.aclexplode" in source
    assert 'for privilege in ("USAGE", "CREATE")' in prepare
    assert "GRANT {privilege} ON SCHEMA app_private" in prepare
    assert "TO app_security_owner" in prepare
    assert '"added": tuple(added)' in prepare
    assert 'for privilege in reversed(state["added"])' in restore
    assert "REVOKE {privilege} ON SCHEMA app_private" in restore
    assert "FROM app_security_owner" in restore
    assert 'state["before"][privilege]' in restore
    assert 'state["effective_before"][privilege]' in restore
    assert 'state["public_before"]' in restore
    assert replace.index("_a1_prepare_app_private_owner_window") < replace.index(
        "_a1_run_as_security_owner"
    )
    assert replace.index("_a1_run_as_security_owner") < replace.index(
        "_a1_restore_app_private_owner_window"
    )
    assert (
        "REVOKE ALL ON FUNCTION "
        "app_private.ensure_future_partition(TEXT, INTEGER) FROM PUBLIC"
        in replace_literals
    )
    for direction in ("upgrade", "downgrade"):
        assert not _op_mutation_lines(
            A1, direction, touching=("ensure_future_partition",)
        )


def test_a1_partition_function_restores_exact_0026_predecessor() -> None:
    source = _literal_text(A1)
    for token in (
        "CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(",
        "p_table_name TEXT",
        "p_days_ahead INT",
        "RETURNS VOID STRICT VOLATILE SECURITY DEFINER",
        "SET search_path = pg_catalog, public",
        "WHEN 'branch_audit_log' THEN 'public.branch_audit_log'",
        "WHEN 'auth_sessions' THEN 'public.auth_sessions'",
        "USING ERRCODE = 'invalid_parameter_value'",
        "Allowed: branch_audit_log, auth_sessions.",
    ):
        assert token in source
    assert "SET search_path = pg_catalog LANGUAGE plpgsql" in source
    verifier = _normalized(_function_source(A1, "_a1_verify_function_contract"))
    assert '"non_owner_execute": False' in verifier
    upgrade = _normalized(_function_source(A1, "upgrade"))
    downgrade = _normalized(_function_source(A1, "downgrade"))
    assert '_a1_replace_partition_function(bind, "forward")' in upgrade
    assert '_a1_replace_partition_function(bind, "predecessor")' in downgrade


def test_a1_downgrade_preserves_forced_rls_and_exact_policy() -> None:
    source = _source(A1)
    downgrade = _reachable_source(A1, "downgrade")
    assert "DISABLE ROW LEVEL SECURITY" not in source
    assert "DROP POLICY" not in downgrade
    assert "CREATE POLICY" not in downgrade
    preflight = _reachable_source(A1, "_a1_preflight")
    for table in (
        "branch_staff_roles",
        "organization_members",
        "branch_audit_log",
    ):
        assert table in preflight
    for token in (
        "tenant_isolation_staff_roles",
        "pg_get_expr",
        "app.current_org_id",
        "app.can_read_staff_roles",
        "deleted_at is null",
    ):
        assert token in preflight


def test_a1_downgrade_removes_only_revision_added_indexes() -> None:
    downgrade = _normalized(_reachable_source(A1, "downgrade"))
    for index_name in (
        "ix_auth_sessions_active",
        "ix_snapshot_active",
        "ix_member_active",
        "ix_roles_active_lookup",
    ):
        assert re.search(
            rf"DROP INDEX(?: IF EXISTS)?(?: public\.)?{index_name}\b",
            downgrade,
            re.IGNORECASE,
        )
    assert not re.search(
        r"DROP INDEX(?: IF EXISTS)?(?: public\.)?ix_audit_org_sequence\b",
        downgrade,
        re.IGNORECASE,
    )
    assert "ix_audit_org_sequence" in _reachable_source(A1, "_a1_preflight")


def test_361_catalog_checks_are_read_only_and_bracket_both_directions() -> None:
    functions = _functions(M361)
    assert {"_preflight", "_postflight", "_verify_contract"} <= set(functions)
    for name in ("_preflight", "_postflight"):
        _assert_read_only_preflight(M361, name)
    closure = "\n".join(
        _reachable_source(M361, name)
        for name in ("_preflight", "_postflight")
    )
    for token in (
        "session_user",
        "current_user",
        "migration_owner",
        "pg_class",
        "pg_namespace",
        "pg_attribute",
        "pg_constraint",
        "org_branches",
        "countries",
        "subdivisions",
        "cities",
        "postal_codes",
    ):
        assert token in closure
    source = _source(M361)
    for token in (*GEO_COLUMNS, *(item[0] for item in GEO_FOREIGN_KEYS)):
        assert token in source
    for direction in ("upgrade", "downgrade"):
        preflight = _call_lines(M361, direction, {"_preflight"})
        postflight = _call_lines(M361, direction, {"_postflight"})
        mutations = _op_mutation_lines(M361, direction)
        assert len(preflight) == len(postflight) == 1
        assert mutations
        assert preflight[0] < min(mutations)
        assert max(mutations) < postflight[0]


def test_361_upgrade_is_exact_nullable_geo_delta() -> None:
    assert _module_literal_assignment(M361, "_SCHEMA") == "public"
    assert _module_literal_assignment(M361, "_GEO_COLUMNS") == {
        name: catalog_type
        for name, (catalog_type, _sa_type) in GEO_COLUMNS.items()
    }
    assert _module_literal_assignment(M361, "_GEO_FOREIGN_KEYS") == (
        GEO_FOREIGN_KEYS
    )
    calls = _direct_op_calls(M361, "upgrade")
    assert [call.func.attr for call in calls] == [
        "add_column",
        "add_column",
        "add_column",
        "add_column",
        "create_foreign_key",
    ]
    for call, (name, (_catalog_type, sa_type)) in zip(
        calls[:4], GEO_COLUMNS.items(), strict=True
    ):
        assert ast.literal_eval(call.args[0]) == "org_branches"
        column = call.args[1]
        assert isinstance(column, ast.Call)
        assert isinstance(column.func, ast.Attribute)
        assert column.func.attr == "Column"
        assert ast.literal_eval(column.args[0]) == name
        assert _attribute_name(column.args[1]) == sa_type
        assert ast.literal_eval(_keyword(column, "nullable")) is True
        assert ast.unparse(_keyword(call, "schema")) == "_SCHEMA"
    foreign_key = calls[-1]
    assert ast.unparse(foreign_key.args[0]) == "name"
    assert ast.literal_eval(foreign_key.args[1]) == "org_branches"
    assert ast.unparse(foreign_key.args[2]) == "parent_table"
    assert ast.unparse(foreign_key.args[3]) == "list(local_columns)"
    assert ast.unparse(foreign_key.args[4]) == "list(parent_columns)"
    assert ast.unparse(_keyword(foreign_key, "source_schema")) == "_SCHEMA"
    assert ast.unparse(_keyword(foreign_key, "referent_schema")) == "_SCHEMA"
    assert ast.literal_eval(_keyword(foreign_key, "ondelete")) == "RESTRICT"


def test_361_parent_key_preflight_matches_every_geo_fk() -> None:
    assert _module_literal_assignment(M361, "_REQUIRED_PARENT_COLUMNS") == {
        ("countries", "id"): "smallint",
        ("subdivisions", "id"): "bigint",
        ("subdivisions", "country_id"): "smallint",
        ("cities", "id"): "bigint",
        ("cities", "country_id"): "smallint",
        ("postal_codes", "id"): "bigint",
    }
    required_keys = {
        (parent_table, parent_columns)
        for _name, _local, parent_table, parent_columns in GEO_FOREIGN_KEYS
    }
    assert _module_literal_assignment(M361, "_REQUIRED_PARENT_KEYS") == (
        required_keys
    )
    preflight = _reachable_source(M361, "_preflight")
    for token in (
        "constraint_type",
        'in {"p", "u"}',
        "available_keys",
        "missing_keys",
        "_REQUIRED_PARENT_KEYS",
    ):
        assert token in preflight


def test_361_downgrade_is_exact_reverse_geo_delta() -> None:
    calls = _direct_op_calls(M361, "downgrade")
    assert [call.func.attr for call in calls] == [
        "drop_constraint",
        "drop_column",
        "drop_column",
        "drop_column",
        "drop_column",
    ]
    drop_constraint = calls[0]
    assert ast.unparse(drop_constraint.args[0]) == "name"
    assert ast.literal_eval(drop_constraint.args[1]) == "org_branches"
    assert ast.literal_eval(_keyword(drop_constraint, "type_")) == "foreignkey"
    assert ast.unparse(_keyword(drop_constraint, "schema")) == "_SCHEMA"
    loops = [
        item
        for item in ast.walk(_functions(M361)["downgrade"])
        if isinstance(item, ast.For)
    ]
    assert len(loops) == 1
    assert isinstance(loops[0].iter, ast.Call)
    assert isinstance(loops[0].iter.func, ast.Name)
    assert loops[0].iter.func.id == "reversed"
    assert ast.unparse(loops[0].iter.args[0]) == "_GEO_FOREIGN_KEYS"
    assert [ast.literal_eval(call.args[1]) for call in calls[1:]] == [
        "geo_postal_code_id",
        "geo_city_id",
        "geo_subdivision_id",
        "geo_country_id",
    ]
    assert all(
        ast.literal_eval(call.args[0]) == "org_branches"
        and ast.unparse(_keyword(call, "schema")) == "_SCHEMA"
        for call in calls[1:]
    )


def test_361_has_no_autogenerate_subtraction_or_data_rebuild() -> None:
    allowed = {
        "add_column",
        "create_foreign_key",
        "drop_constraint",
        "drop_column",
    }
    for direction in ("upgrade", "downgrade"):
        assert {call.func.attr for call in _direct_op_calls(M361, direction)} <= allowed
        for helper in _reachable_names(M361, direction) - {direction}:
            assert _direct_op_calls(M361, helper) == []
        assert _dependent_staff_role_type_change_lines(M361, direction) == []
    literals = _literal_text(M361)
    for pattern in (
        r"\b(?:CREATE|DROP) TABLE\b",
        r"\b(?:CREATE|DROP) INDEX\b",
        r"\b(?:CREATE|DROP) VIEW\b",
        r"\b(?:CREATE|DROP) POLICY\b",
        r"\bALTER COLUMN\b.+?\bTYPE\b",
        r"\bROW LEVEL SECURITY\b",
        r"\bCASCADE\b",
    ):
        assert not re.search(pattern, literals, re.IGNORECASE | re.DOTALL)
    source = _source(M361)
    for predecessor in (
        "member_permission_snapshots",
        "scope_types",
        "staff_roles",
        "membership_statuses",
        "permissions",
        "role_permission_events",
        "effective_role_permissions",
        "encryption_key_registry",
        "address_audit_ledger",
        "idempotency_store",
        "event_outbox",
        "auth_sessions",
        "v_active_org_branches",
    ):
        assert predecessor not in source


def test_361_preserves_canonical_smallint_rbac_lineage() -> None:
    reference_tables = _literal_text(M0023)
    branch_roles = _literal_text(M0025)
    snapshots = _literal_text(M0027)
    assert re.search(r"CREATE TABLE public\.staff_roles \( id SMALLINT PRIMARY KEY", reference_tables)
    assert re.search(r"CREATE TABLE public\.scope_types \( id SMALLINT PRIMARY KEY", reference_tables)
    assert "ADD COLUMN IF NOT EXISTS role_id SMALLINT NULL" in branch_roles
    assert "ADD COLUMN IF NOT EXISTS scope_type_id SMALLINT NOT NULL DEFAULT 2" in branch_roles
    assert "scope_type_id SMALLINT NOT NULL DEFAULT 2" in snapshots
    assert "compile_member_permissions(UUID, UUID, UUID, SMALLINT)" in snapshots
    source = _source(M361)
    for relation in ("branch_staff_roles", "member_permission_snapshots", "scope_types", "staff_roles"):
        assert relation not in source


def test_361_preserves_both_manifest_secure_views_without_ddl() -> None:
    manifest = _normalized(_source(ROOT / "security" / "cluster_role_bootstrap" / "ownership.v1.json"))
    for view in (ACTIVE_VIEW, EFFECTIVE_VIEW):
        assert re.search(
            rf'"object": "{re.escape(view)}", "object_type": "VIEW".+?'
            r'"target_owner": "app_security_owner"',
            manifest,
        )
    replacement_literals = _function_literal_text(A1, "_a1_replace_active_view")
    forward = _module_string_assignment(A1, "_A1_FORWARD_VIEW_SQL")
    _assert_simple_active_view_contract(
        f"{forward} {replacement_literals} {CANONICAL_VIEW_COMMENT}"
    )
    _assert_effective_view_contract(_literal_text(M0027))
    source = _source(M361).lower()
    assert "app_secure" not in source
    assert ACTIVE_VIEW not in source
    assert EFFECTIVE_VIEW not in source
    for base_relation in ("branch_staff_roles", "member_permission_snapshots", "scope_types"):
        assert base_relation not in source


def test_affected_revisions_contain_no_forbidden_security_workaround() -> None:
    forbidden = (
        r"\bALTER ROLE\b.*\b(?:SUPERUSER|BYPASSRLS|INHERIT)\b",
        r"\bGRANT app_security_owner TO migration_owner\b",
        r"\bGRANT CREATE ON SCHEMA app_secure\b",
        r"\bALTER SCHEMA app_secure OWNER TO\b",
        r"\bDROP SCHEMA(?: IF EXISTS)? app_secure\b",
        r"\bDISABLE ROW LEVEL SECURITY\b",
        r"\bsession_replication_role\b",
        r"\bGRANT EXECUTE ON ALL FUNCTIONS\b",
        r"\bGRANT ALL\b",
        r"\bDROP VIEW(?: IF EXISTS)? "
        r"app_secure\.v_active_branch_staff_roles CASCADE\b",
    )
    for path in (A1, M361):
        source = _normalized(_source(path))
        assert "autocommit_block" not in source
        assert "CONCURRENTLY" not in source.upper()
        for pattern in forbidden:
            assert not re.search(pattern, source, re.IGNORECASE | re.DOTALL), (
                path.name,
                pattern,
            )
        role_entries = [
            _normalized(value)
            for value in _string_constants(_tree(path))
            if re.fullmatch(
                r"SET(?: LOCAL)? ROLE app_security_owner;?",
                _normalized(value),
                re.IGNORECASE,
            )
        ]
        if path == A1:
            assert role_entries
            assert all("SET LOCAL ROLE" in entry.upper() for entry in role_entries)
        else:
            assert role_entries == []
