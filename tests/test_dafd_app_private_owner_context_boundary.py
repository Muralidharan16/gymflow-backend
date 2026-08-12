from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
DAFD = VERSIONS / "dafd2b02005e_add_branch_staff_roles_deactivation_.py"
M0021 = VERSIONS / "0021_staff_roles.py"
M0029 = VERSIONS / "0029_rbac_p8_contract.py"
AUTH_RUNTIME_ACL = "5e6f708192a3_auth_runtime_privilege_boundary.py"
LIFECYCLE_MAINTENANCE_ACL = "b5c6d7e8f9a0_bound_lifecycle_maintenance_runtime.py"

CANONICAL_FUNCTION = "app_private.handle_user_deactivation_cascade()"
CANONICAL_TRIGGER = "trg_user_deactivation_cascade"
FORBIDDEN_FUNCTION = "app_private.handle_org_user_deactivation_cascade()"
FORBIDDEN_TRIGGER = "trg_org_user_deactivation_cascade"

APP_PRIVATE_FILES = {
    "0020_contacts_hardened.py",
    "0021_staff_roles.py",
    "0022_rbac_phase1_roles_extensions.py",
    "0024_rbac_p3_org_members.py",
    "0025_rbac_p4_bsr_expand.py",
    "0026_rbac_p5_audit_log.py",
    "0027_rbac_p6_perm_snapshots.py",
    "0028_rbac_p7_role_events.py",
    "0029_rbac_p8_contract.py",
    "45df3b75ed74_rbac_hardening_phase_10_audit_functions.py",
    "4d5e6f708192_establish_audit_principal_registry.py",
    "a1b2c3d4e5f6_rbac_hardening_phase_15_to_18.py",
    "b4c5d6e7f809_harden_branch_hours_runtime_boundary.py",
    DAFD.name,
    "dbeb400472ec_add_branch_operating_hours.py",
    "f71f231fb001_rbac_hardening_phase_10_partitioned_.py",
}

APP_RLS_EXECUTOR_FILES = {
    "0020_contacts_hardened.py",
    "0021_staff_roles.py",
    "0025_rbac_p4_bsr_expand.py",
    "0029_rbac_p8_contract.py",
    AUTH_RUNTIME_ACL,
    LIFECYCLE_MAINTENANCE_ACL,
    DAFD.name,
    "f71f231fb001_rbac_hardening_phase_10_partitioned_.py",
}

APPROVED_NON_PRIVATE_EXECUTOR_FILES = {
    AUTH_RUNTIME_ACL,
    LIFECYCLE_MAINTENANCE_ACL,
}


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
    raise AssertionError(f"Missing module assignment {name}")


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


def _reachable_op_mutations(path: Path, root: str) -> list[ast.Call]:
    result = []
    for name in _reachable_names(path, root):
        for item in ast.walk(_functions(path)[name]):
            if not isinstance(item, ast.Call):
                continue
            if not isinstance(item.func, ast.Attribute):
                continue
            if not isinstance(item.func.value, ast.Name):
                continue
            if item.func.value.id != "op":
                continue
            if item.func.attr in {"get_bind", "get_context"}:
                continue
            result.append(item)
    return sorted(result, key=lambda item: item.lineno)


def _module_string_values(path: Path) -> dict[str, str]:
    result = {}
    for node in _tree(path).body:
        value: ast.expr | None = None
        names: list[str] = []
        if isinstance(node, ast.Assign):
            names = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names = [node.target.id]
            value = node.value
        if value is None:
            continue
        try:
            observed = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
        if isinstance(observed, str):
            for name in names:
                result[name] = observed
    return result


def _resolved_string(path: Path, node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return _module_string_values(path).get(node.id)
    if isinstance(node, ast.Call) and node.args:
        return _resolved_string(path, node.args[0])
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _reachable_execute_sql(path: Path, root: str) -> list[str]:
    result = []
    for name in _reachable_names(path, root):
        for item in ast.walk(_functions(path)[name]):
            if not isinstance(item, ast.Call):
                continue
            if not isinstance(item.func, ast.Attribute):
                continue
            if item.func.attr not in {"execute", "exec_driver_sql", "scalar"}:
                continue
            assert item.args, f"SQL call at line {item.lineno} has no statement"
            sql = _resolved_string(path, item.args[0])
            assert sql is not None, (
                f"SQL call at line {item.lineno} is not statically auditable"
            )
            result.append(_normalized(sql))
    return result


def _ddl_occurrences(pattern: str) -> list[str]:
    result = []
    compiled = re.compile(pattern, re.IGNORECASE)
    for path in sorted(VERSIONS.glob("*.py")):
        for literal in _string_constants(_tree(path)):
            result.extend(path.name for _ in compiled.finditer(literal))
    return result


def _canonical_0029_function_definition() -> str:
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
        r"app_private\.handle_user_deactivation_cascade\s*\(\s*\)",
        re.IGNORECASE,
    )
    return next(
        literal
        for literal in _string_constants(_tree(M0029))
        if pattern.search(literal)
    )


def _canonical_0029_prosrc() -> str:
    definition = _canonical_0029_function_definition()
    match = re.search(
        r"\bAS\s+\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\$"
        r"(?P<body>.*?)\$(?P=tag)\$",
        definition,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _app_private_ddl_categories(path: Path) -> set[str]:
    literals = _string_constants(_tree(path))
    text = _normalized("\n".join(literals))
    patterns = {
        "create_schema": (
            r"\bCREATE\s+SCHEMA(?:\s+IF\s+NOT\s+EXISTS)?\s+app_private\b"
        ),
        "grant_schema": r"\bGRANT\b[^;]*\bON\s+SCHEMA\s+app_private\b",
        "revoke_schema": r"\bREVOKE\b[^;]*\bON\s+SCHEMA\s+app_private\b",
        "comment_schema": r"\bCOMMENT\s+ON\s+SCHEMA\s+app_private\b",
        "drop_schema": (
            r"\bDROP\s+SCHEMA(?:\s+IF\s+EXISTS)?\s+app_private\b"
        ),
        "create_private_table": (
            r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+app_private\."
        ),
        "alter_private_table": r"\bALTER\s+TABLE\s+app_private\.",
        "drop_private_table": (
            r"\bDROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+app_private\."
        ),
        "create_function": (
            r"\bCREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+app_private\."
        ),
        "alter_function": r"\bALTER\s+FUNCTION\s+app_private\.",
        "drop_function": (
            r"\bDROP\s+FUNCTION(?:\s+IF\s+EXISTS)?\s+app_private\."
        ),
        "grant_function": (
            r"\bGRANT\b.*\bON\s+FUNCTION\s+app_private\."
        ),
        "revoke_function": (
            r"\bREVOKE\b[^;]*\bON\s+FUNCTION\s+app_private\."
        ),
        "comment_function": r"\bCOMMENT\s+ON\s+FUNCTION\s+app_private\.",
        "create_trigger": (
            r"\bCREATE\s+TRIGGER\b[^;]*"
            r"\bEXECUTE\s+FUNCTION\s+app_private\."
        ),
        "executor_owner": r"\bOWNER\s+TO\s+app_rls_executor\b",
        "executor_set_role": (
            r"\bSET(?:\s+LOCAL)?\s+ROLE\s+app_rls_executor\b"
        ),
        "executor_grant": r"\bGRANT\b[^;]*\bTO\s+app_rls_executor\b",
        "executor_revoke": (
            r"\bREVOKE\b[^;]*\bFROM\s+app_rls_executor\b"
        ),
    }
    categories = {
        category
        for category, pattern in patterns.items()
        if re.search(pattern, text, re.IGNORECASE)
    }
    policy_literals = [
        literal
        for literal in literals
        if re.search(r"\bCREATE\s+POLICY\b", literal, re.IGNORECASE)
    ]
    direct_executor_policy = any(
        re.search(r"\bapp_rls_executor\b", literal, re.IGNORECASE)
        for literal in policy_literals
    )
    executor_policy_context = bool(policy_literals) and bool(
        re.search(
            r"\bSET(?:\s+LOCAL)?\s+ROLE\s+app_rls_executor\b",
            text,
            re.IGNORECASE,
        )
    )
    if direct_executor_policy or executor_policy_context:
        categories.add("executor_policy")
    return categories


def test_complete_95_revision_private_and_executor_inventories_are_closed() -> None:
    migrations = sorted(VERSIONS.glob("*.py"))
    assert len(migrations) == 95
    app_private = {
        path.name
        for path in migrations
        if "app_private" in _source(path).lower()
    }
    app_rls_executor = {
        path.name
        for path in migrations
        if "app_rls_executor" in _source(path).lower()
    }
    assert app_private == APP_PRIVATE_FILES
    assert app_rls_executor == APP_RLS_EXECUTOR_FILES
    assert app_rls_executor - app_private == APPROVED_NON_PRIVATE_EXECUTOR_FILES
    assert (
        app_rls_executor - APPROVED_NON_PRIVATE_EXECUTOR_FILES
    ) <= app_private
    for name in APPROVED_NON_PRIVATE_EXECUTOR_FILES:
        assert _app_private_ddl_categories(VERSIONS / name) == set()


def test_complete_app_private_ddl_category_allowlist_is_exact() -> None:
    expected = {
        "0020_contacts_hardened.py": {
            "alter_function",
            "alter_private_table",
            "create_function",
            "create_private_table",
            "create_schema",
            "create_trigger",
            "drop_function",
            "drop_private_table",
            "drop_schema",
            "executor_grant",
            "executor_owner",
            "executor_policy",
            "executor_revoke",
            "executor_set_role",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0021_staff_roles.py": {
            "alter_function",
            "create_function",
            "create_trigger",
            "drop_function",
            "executor_grant",
            "executor_owner",
            "executor_policy",
            "executor_revoke",
            "executor_set_role",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0022_rbac_phase1_roles_extensions.py": set(),
        "0024_rbac_p3_org_members.py": {
            "alter_function",
            "comment_function",
            "create_function",
            "create_trigger",
            "drop_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0025_rbac_p4_bsr_expand.py": {
            "alter_function",
            "create_function",
            "create_private_table",
            "create_trigger",
            "drop_function",
            "drop_private_table",
            "executor_policy",
            "executor_set_role",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0026_rbac_p5_audit_log.py": {
            "alter_function",
            "create_function",
            "create_trigger",
            "drop_function",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0027_rbac_p6_perm_snapshots.py": {
            "alter_function",
            "comment_function",
            "create_function",
            "create_trigger",
            "drop_function",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "0028_rbac_p7_role_events.py": {
            "alter_function",
            "create_function",
            "create_trigger",
            "grant_function",
            "revoke_function",
        },
        "0029_rbac_p8_contract.py": {
            "create_function",
            "create_private_table",
            "create_trigger",
            "drop_function",
            "drop_private_table",
            "executor_policy",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "45df3b75ed74_rbac_hardening_phase_10_audit_functions.py": {
            "create_function",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "4d5e6f708192_establish_audit_principal_registry.py": {
            "create_function",
            "create_trigger",
            "drop_function",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "a1b2c3d4e5f6_rbac_hardening_phase_15_to_18.py": {
            "create_function",
            "grant_function",
            "grant_schema",
            "revoke_function",
            "revoke_schema",
        },
        "b4c5d6e7f809_harden_branch_hours_runtime_boundary.py": {
            "create_trigger",
        },
        DAFD.name: set(),
        "dbeb400472ec_add_branch_operating_hours.py": {
            "create_function",
            "create_trigger",
            "drop_function",
        },
        "f71f231fb001_rbac_hardening_phase_10_partitioned_.py": set(),
    }
    actual = {
        name: _app_private_ddl_categories(VERSIONS / name)
        for name in APP_PRIVATE_FILES
    }
    assert actual == expected


def test_canonical_function_and_trigger_have_one_authoritative_lineage() -> None:
    function_pattern = (
        r"\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
        r"app_private\.handle_user_deactivation_cascade\s*\(\s*\)"
    )
    trigger_pattern = (
        r"\bCREATE\s+TRIGGER\s+trg_user_deactivation_cascade\b"
    )
    owner_pattern = (
        r"\bALTER\s+FUNCTION\s+"
        r"app_private\.handle_user_deactivation_cascade\s*\(\s*\)\s+"
        r"OWNER\s+TO\s+app_rls_executor\b"
    )
    assert _ddl_occurrences(function_pattern) == [M0021.name, M0029.name]
    assert _ddl_occurrences(trigger_pattern) == [M0021.name]
    assert _ddl_occurrences(owner_pattern) == [M0021.name]

    trigger_sql = next(
        literal
        for literal in _string_constants(_tree(M0021))
        if re.search(trigger_pattern, literal, re.IGNORECASE)
    )
    normalized = _normalized(trigger_sql)
    for token in (
        "AFTER UPDATE OF is_active ON public.organization_users",
        "FOR EACH ROW",
        "WHEN (NEW.is_active = FALSE)",
        f"EXECUTE FUNCTION {CANONICAL_FUNCTION}",
    ):
        assert token in normalized


def test_0029_freezes_the_canonical_function_security_contract() -> None:
    owners = _module_literal_assignment(M0029, "_FUNCTION_OWNERS")
    assert owners[CANONICAL_FUNCTION] == "app_rls_executor"
    definition = next(
        literal
        for literal in _string_constants(_tree(M0029))
        if re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
            r"app_private\.handle_user_deactivation_cascade\s*\(\s*\)",
            literal,
            re.IGNORECASE,
        )
    )
    normalized = _normalized(definition)
    for token in (
        "RETURNS TRIGGER",
        "LANGUAGE plpgsql",
        "SECURITY DEFINER",
        "SET search_path TO 'pg_catalog'",
        "SET row_security TO 'on'",
        "app.current_org_id",
        "app.current_user_id",
        "NEW.org_id IS DISTINCT FROM v_context_org",
        "FROM public.organization_members AS om",
        "SET revoked_at = clock_timestamp(), revoked_by = v_actor_member",
        "target_member.user_id = NEW.id",
        "target_member.org_id = NEW.org_id",
        "bsr.revoked_at IS NULL",
        "bsr.deleted_at IS NULL",
    ):
        assert token in normalized


def test_duplicate_objects_are_collision_literals_only_and_never_ddl() -> None:
    migrations = sorted(VERSIONS.glob("*.py"))
    duplicate_holders = {
        path.name
        for path in migrations
        if FORBIDDEN_FUNCTION.removesuffix("()") in _source(path)
        or FORBIDDEN_TRIGGER in _source(path)
    }
    assert duplicate_holders == {DAFD.name}

    all_literals = "\n".join(
        literal
        for path in migrations
        for literal in _string_constants(_tree(path))
    )
    forbidden_actions = (
        r"\b(?:CREATE(?:\s+OR\s+REPLACE)?|ALTER|DROP)\s+FUNCTION\s+"
        r"app_private\.handle_org_user_deactivation_cascade\s*\(\s*\)",
        r"\b(?:CREATE|DROP)\s+TRIGGER\s+trg_org_user_deactivation_cascade\b",
        r"\bEXECUTE\s+FUNCTION\s+"
        r"app_private\.handle_org_user_deactivation_cascade\s*\(\s*\)",
    )
    for pattern in forbidden_actions:
        assert not re.search(pattern, all_literals, re.IGNORECASE)


def test_dafd_is_revision_local_validation_only_in_both_directions() -> None:
    assert _module_literal_assignment(DAFD, "revision") == "dafd2b02005e"
    assert _module_literal_assignment(DAFD, "down_revision") == "b2c3d4e5f6a1"
    source = _source(DAFD)
    assert "from alembic.versions" not in source
    assert not re.search(r"(?:from|import)\s+app(?:\.|\s|$)", source)
    assert _module_literal_assignment(DAFD, "_CANONICAL_FUNCTION") == (
        "handle_user_deactivation_cascade"
    )
    assert _module_literal_assignment(DAFD, "_DUPLICATE_FUNCTION") == (
        "handle_org_user_deactivation_cascade"
    )
    assert _module_literal_assignment(DAFD, "_CANONICAL_TRIGGER") == (
        CANONICAL_TRIGGER
    )
    assert _module_literal_assignment(DAFD, "_DUPLICATE_TRIGGER") == (
        FORBIDDEN_TRIGGER
    )

    functions = _functions(DAFD)
    validator = "_validate_canonical_deactivation_contract"
    assert validator in functions
    for direction in ("upgrade", "downgrade"):
        calls = [name for _, name in _direct_module_calls(DAFD, direction)]
        assert calls == [validator]
        assert validator in _reachable_names(DAFD, direction)
        assert _reachable_op_mutations(DAFD, direction) == []
        entrypoint = _normalized(_function_source(DAFD, direction))
        assert f"{validator}(op.get_bind())" in entrypoint


def test_dafd_reachable_sql_is_catalog_select_only() -> None:
    upgrade_sql = _reachable_execute_sql(DAFD, "upgrade")
    downgrade_sql = _reachable_execute_sql(DAFD, "downgrade")
    assert upgrade_sql
    assert sorted(upgrade_sql) == sorted(downgrade_sql)
    for sql in upgrade_sql:
        assert re.match(r"^(?:SELECT|WITH)\b", sql, re.IGNORECASE)
        assert not re.search(
            r"(?:^|;)\s*(?:CREATE|ALTER|DROP|GRANT|REVOKE|COMMENT|"
            r"SET|RESET|INSERT|UPDATE|DELETE|TRUNCATE|DO|CALL)\b",
            sql,
            re.IGNORECASE,
        )


def test_dafd_validates_exact_function_owner_body_and_acl_contract() -> None:
    reachable = _reachable_names(DAFD, "upgrade")
    literals = _normalized(
        "\n".join(
            literal
            for name in reachable
            for literal in _string_constants(_functions(DAFD)[name])
        )
    )
    for token in (
        "session_user",
        "current_user",
        "migration_owner",
        "pg_catalog.pg_has_role",
        "pg_catalog.pg_roles",
        "pg_catalog.pg_namespace",
        "pg_catalog.aclexplode",
        "pg_catalog.pg_proc",
        "prokind",
        "prorettype",
        "prosecdef",
        "provolatile",
        "proisstrict",
        "proparallel",
        "proconfig",
        "prosrc",
        "app_rls_executor",
        "EXECUTE",
    ):
        assert token.lower() in literals.lower()

    function_names = {
        node.id
        for node in ast.walk(_functions(DAFD)["_require_canonical_function"])
        if isinstance(node, ast.Name)
    }
    assert "_CANONICAL_FUNCTION" in function_names
    assert "_CANONICAL_PROSRC_SHA256" in function_names

    acl = _normalized(_function_source(DAFD, "_require_function_acl"))
    assert "pg_catalog.aclexplode" in acl
    assert "pg_catalog.acldefault('f', routine.proowner)" in acl
    assert (
        'expected = ((_RLS_EXECUTOR, "EXECUTE", False, _RLS_EXECUTOR),)'
        in acl
    )

    predecessor_body = _normalized(_canonical_0029_prosrc())
    expected_digest = hashlib.sha256(
        predecessor_body.encode("utf-8")
    ).hexdigest()
    assert _module_literal_assignment(
        DAFD, "_CANONICAL_PROSRC_SHA256"
    ) == expected_digest


def test_dafd_validates_exact_trigger_rls_and_collision_contract() -> None:
    trigger_node = _functions(DAFD)["_require_canonical_trigger"]
    validator = _normalized("\n".join(_string_constants(trigger_node)))
    for token in (
        "pg_catalog.pg_trigger",
        "pg_catalog.pg_get_triggerdef",
        "tgtype",
        "tgattr",
        "tgqual",
        "tgfoid",
        "tgenabled",
        "public",
        "organization_users",
        "relrowsecurity",
        "relforcerowsecurity",
        "is_active",
    ):
        assert token.lower() in validator.lower()

    assert "pg_catalog.pg_get_expr" not in validator.lower()

    names = {
        node.id
        for node in ast.walk(trigger_node)
        if isinstance(node, ast.Name)
    }
    assert "_CANONICAL_TRIGGER" in names
    trigger_source = _normalized(
        _function_source(DAFD, "_require_canonical_trigger")
    )
    for token in (
        '"enabled_state": "O"',
        '"is_internal": False',
        '"constraint_oid": 0',
        '"trigger_type": 17',
        '"relation_schema": "public"',
        '"relation_name": "organization_users"',
        '"relation_owner": _MIGRATION_OWNER',
        '"rls_enabled": True',
        '"force_rls": True',
        'tuple(row["update_columns"]) != ("is_active",)',
        'if not row["has_when_predicate"]',
        "pg_catalog.pg_get_triggerdef",
        "trigger_definition_match = re.fullmatch",
        'predicate != "new.is_active=false"',
    ):
        assert token in trigger_source

    assert "pg_catalog.pg_get_expr" not in trigger_source
    assert "pg_catalog.pg_get_triggerdef" in trigger_source

    duplicate_node = _functions(DAFD)["_require_duplicate_pair_absent"]
    duplicate_names = {
        node.id
        for node in ast.walk(duplicate_node)
        if isinstance(node, ast.Name)
    }
    assert "_DUPLICATE_FUNCTION" in duplicate_names
    assert "_DUPLICATE_TRIGGER" in duplicate_names
    source = _source(DAFD)
    assert CANONICAL_TRIGGER in source
    assert FORBIDDEN_TRIGGER in source
    assert _module_literal_assignment(DAFD, "_PRIVATE_SCHEMA") == "app_private"
    assert _module_literal_assignment(DAFD, "_CANONICAL_FUNCTION") == (
        "handle_user_deactivation_cascade"
    )
    assert _module_literal_assignment(DAFD, "_DUPLICATE_FUNCTION") == (
        "handle_org_user_deactivation_cascade"
    )


def test_dafd_has_no_security_or_ownership_workaround() -> None:
    source = _literal_text(DAFD)
    forbidden = (
        r"\bCREATE\s+(?:ROLE|USER|SCHEMA|TABLE|FUNCTION|TRIGGER|POLICY)\b",
        r"\bALTER\s+(?:ROLE|USER|SCHEMA|TABLE|FUNCTION|POLICY)\b",
        r"\bDROP\s+(?:ROLE|USER|SCHEMA|TABLE|FUNCTION|TRIGGER|POLICY|OWNED)\b",
        r"\bGRANT\b[^;]*\b(?:TO|ON)\b",
        r"\bREVOKE\b[^;]*\b(?:FROM|ON)\b",
        r"\bSET(?:\s+LOCAL)?\s+ROLE\b",
        r"\bRESET\s+ROLE\b",
        r"\bSET\s+SESSION\s+AUTHORIZATION\b",
        r"\bsession_replication_role\b",
        r"\brow_security\s*(?:=|TO)\s*'?off'?\b",
        r"\b(?:DISABLE|ENABLE)\s+TRIGGER\b",
        r"\b(?:DISABLE|ENABLE|FORCE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bALTER\s+ROLE\b[^;]*\b(?:SUPERUSER|BYPASSRLS)\b",
        r"\bCASCADE\b",
    )
    for pattern in forbidden:
        assert not re.search(pattern, source, re.IGNORECASE)
    assert _app_private_ddl_categories(DAFD) == set()
