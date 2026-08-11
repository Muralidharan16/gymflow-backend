from __future__ import annotations

import ast
import re
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/d6e7f8091a2b_align_branch_hours_typed_principals.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _module() -> ast.Module:
    return ast.parse(_source(), filename=str(MIGRATION))


def _function_source(name: str) -> str:
    source = _source()
    node = next(
        item
        for item in _module().body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def _sql_literals() -> list[str]:
    result = []
    for node in ast.walk(_module()):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        normalized = re.sub(r"\s+", " ", node.value).strip()
        if re.match(
            r"^(?:CREATE|ALTER|DROP|GRANT|REVOKE|COMMENT|SET|RESET|DO|SELECT|WITH|UPDATE|INSERT|DELETE)\b",
            normalized,
            re.IGNORECASE,
        ):
            result.append(normalized)
    return result


def test_revision_is_forward_only_after_partition_adoption() -> None:
    source = _source()
    assert 'revision = "d6e7f8091a2b"' in source
    assert 'down_revision = "c5d6e7f8091a"' in source


def test_nonmember_principal_validation_is_internal_and_session_bound() -> None:
    create = _function_source("_create_nonmember_validator")
    source = _source()

    assert "CREATE FUNCTION public.branch_hours_current_nonmember_principal_valid" in create
    assert "SECURITY DEFINER" in create
    assert "STABLE" in create
    assert "SET search_path = pg_catalog, public" in create
    assert "SET row_security = on" in create
    assert "app.current_principal_type" in create
    assert "app.current_user_id" in create
    assert "app.current_org_id" in create
    assert "app.current_role" in create
    assert "p_org_id IS DISTINCT FROM v_context_org" in create
    assert "FROM public.owners AS owner_data" in create
    assert "owner_data.email_verified IS TRUE" in create
    assert "owner_data.onboarding_completed IS TRUE" in create
    assert "FROM public.gym_owners AS staff_data" in create
    assert "staff_data.is_active IS TRUE" in create
    assert "staff_data.is_verified IS TRUE" in create
    assert "staff_data.role::text = v_role" in create

    assert "REVOKE ALL ON FUNCTION" in create
    assert "FROM PUBLIC" in create
    assert "GRANT EXECUTE ON FUNCTION" in create
    assert "TO app_runtime" in create
    assert "ALTER FUNCTION public.branch_hours_current_nonmember_principal_valid(uuid)" in create
    assert "OWNER TO app_security_owner" in create
    assert "GRANT CREATE ON SCHEMA public TO app_security_owner" in create
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in create

    # Runtime never receives source-registry SELECT merely to evaluate RLS.
    assert "GRANT SELECT ON TABLE public.owners TO app_runtime" not in source
    assert "GRANT SELECT ON TABLE public.gym_owners TO app_runtime" not in source


def test_security_owner_gets_only_revision_required_validation_columns() -> None:
    create = _function_source("_create_nonmember_validator")
    drop = _function_source("_drop_nonmember_validator")

    assert (
        "GRANT SELECT (id, org_id, email_verified, onboarding_completed)"
        in create
    )
    assert "ON TABLE public.owners" in create
    assert "GRANT SELECT (is_active, is_verified)" in create
    assert "ON TABLE public.gym_owners" in create

    assert (
        "REVOKE SELECT (id, org_id, email_verified, onboarding_completed)"
        in drop
    )
    assert "REVOKE SELECT (is_active, is_verified)" in drop

    # 8192 owns the existing gym_owners id/org_id/role grant; d6 must not revoke it.
    assert "REVOKE SELECT (id, org_id, role)" not in drop


def test_owner_and_legacy_policy_expressions_call_validator_not_source_tables() -> None:
    owner = _function_source("_active_owner_expr")
    legacy = _function_source("_active_legacy_staff_expr")

    assert "branch_hours_current_nonmember_principal_valid" in owner
    assert "branch_hours_current_nonmember_principal_valid" in legacy
    assert "FROM public.owners" not in owner
    assert "FROM public.gym_owners" not in legacy
    assert "= 'owner'" in owner
    assert "= 'legacy_gym_owner'" in legacy


def test_modern_principal_stays_under_normal_membership_rls() -> None:
    modern = _function_source("_active_org_user_expr")
    branch_write = _function_source("_branch_write_expr")

    assert "= 'organization_user'" in modern
    assert "FROM public.organization_members AS member_data" in modern
    assert "member_data.membership_status_id = 3" in modern
    assert "member_data.deleted_at IS NULL" in modern

    for token in (
        "organization_user",
        "public.organization_members",
        "public.branch_staff_roles",
        "organization_member_id",
        "role_assignment.role_id = 3",
        "role_assignment.revoked_at IS NULL",
        "role_assignment.deleted_at IS NULL",
        "member_data.membership_status_id = 3",
        "member_data.deleted_at IS NULL",
    ):
        assert token in branch_write


def test_role_string_alone_never_authorizes_org_write() -> None:
    org_write = _function_source("_org_write_expr")
    assert "_active_owner_expr" in org_write
    assert "_active_legacy_staff_expr" in org_write
    assert "organization_user" not in org_write
    assert "IN ('owner', 'admin')" in org_write


def test_cross_tenant_branch_state_and_active_state_remain_required() -> None:
    for name in ("_branch_read_expr", "_branch_write_expr"):
        body = _function_source(name)
        assert "public.org_branches" in body
        assert "public.org_branch_state" in body
        assert "branch_data.org_id =" in body
        assert "app.current_org_id" in body
        assert "branch_state.deleted_at IS NULL" in body
        assert "branch_state.is_active IS TRUE" in body


def test_preflight_rejects_runtime_registry_select_and_privileged_security_owner() -> None:
    preflight = _function_source("_require_preflight")

    assert "rolbypassrls" in preflight
    assert "rolcanlogin" in preflight
    assert "rolsuper" in preflight
    assert "app_security_owner must remain NOLOGIN/NOSUPERUSER/NOBYPASSRLS" in preflight
    assert "app_runtime SELECT" in preflight
    assert "public.owners" in preflight
    assert "public.gym_owners" in preflight
    assert "8192 bounded gym_owners identity columns" in preflight


def test_revision_has_no_runtime_privilege_escape() -> None:
    # Column-level SELECT to app_security_owner and function EXECUTE to app_runtime
    # are deliberate. Reject broad/source-table grants or role/RLS bypasses.
    forbidden_sql = (
        r"\bALTER\s+ROLE\b.*\b(?:SUPERUSER|BYPASSRLS|INHERIT|CREATEDB|CREATEROLE)\b",
        r"\bCREATE\s+(?:ROLE|USER)\b",
        r"\bGRANT\s+ALL\b",
        r"\bGRANT\s+SELECT\s+ON\s+TABLE\s+public\.(?:owners|gym_owners)\s+TO\s+app_runtime\b",
        r"\bGRANT\s+(?:INSERT|UPDATE|DELETE|TRUNCATE|REFERENCES|TRIGGER)\b.*\bTO\s+app_runtime\b",
        r"\bDISABLE\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bNO\s+FORCE\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bALTER\s+TABLE\b.*\bOWNER\s+TO\b",
    )
    for sql in _sql_literals():
        for pattern in forbidden_sql:
            assert not re.search(pattern, sql, re.IGNORECASE | re.DOTALL), sql


def test_forward_verifier_closes_function_acl_and_schema_window() -> None:
    verify = _function_source("_verify_forward")

    assert "owner_name" in verify
    assert "security_definer" in verify
    assert "volatility" in verify
    assert "runtime_execute" in verify
    assert "public_execute" in verify
    assert "search_path=pg_catalog, public" in verify
    assert "row_security=on" in verify
    assert "leaked source-registry SELECT to app_runtime" in verify
    assert "left app_security_owner with public CREATE" in verify


def test_downgrade_restores_member_only_b4_policy_shape_and_removes_validator() -> None:
    source = _source()
    restore = _function_source("_restore_b4_policies")
    member = _function_source("_b4_active_member_expr")
    branch = _function_source("_b4_branch_member_expr")
    drop = _function_source("_drop_nonmember_validator")

    assert "public.organization_members" in member
    assert "membership_status_id = 3" in member
    assert "public.branch_staff_roles" in branch
    assert "role_assignment.role_id = 3" in branch
    assert "_b4_active_member_expr" in restore
    assert "_b4_branch_member_expr" in restore
    assert "SET LOCAL ROLE app_security_owner" in drop
    assert "DROP FUNCTION public.branch_hours_current_nonmember_principal_valid(uuid)" in drop
    assert "RESET ROLE" in drop
    assert "_drop_nonmember_validator()" in source
