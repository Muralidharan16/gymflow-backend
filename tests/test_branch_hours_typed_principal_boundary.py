from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/d6e7f8091a2b_align_branch_hours_typed_principals.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    module = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_revision_is_forward_only_after_partition_adoption() -> None:
    source = _source()
    assert 'revision = "d6e7f8091a2b"' in source
    assert 'down_revision = "c5d6e7f8091a"' in source


def test_typed_principal_domains_are_source_validated() -> None:
    source = _source()
    owner = _function_source("_active_owner_expr")
    modern = _function_source("_active_org_user_expr")
    legacy = _function_source("_active_legacy_staff_expr")

    assert "app.current_principal_type" in source

    assert "= 'owner'" in owner
    assert "FROM public.owners AS owner_data" in owner
    assert "owner_data.email_verified IS TRUE" in owner
    assert "owner_data.onboarding_completed IS TRUE" in owner
    assert "app.current_role" in owner

    assert "= 'organization_user'" in modern
    assert "FROM public.organization_members AS member_data" in modern
    assert "member_data.membership_status_id = 3" in modern
    assert "member_data.deleted_at IS NULL" in modern

    assert "= 'legacy_gym_owner'" in legacy
    assert "FROM public.gym_owners AS staff_data" in legacy
    assert "staff_data.is_active IS TRUE" in legacy
    assert "staff_data.is_verified IS TRUE" in legacy
    assert "staff_data.role::text" in legacy


def test_role_string_alone_never_authorizes_org_write() -> None:
    org_write = _function_source("_org_write_expr")
    assert "_active_owner_expr" in org_write
    assert "_active_legacy_staff_expr" in org_write
    assert "organization_user" not in org_write
    assert "IN ('owner', 'admin')" in org_write


def test_modern_manager_requires_active_membership_and_branch_assignment() -> None:
    branch_write = _function_source("_branch_write_expr")
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


def test_cross_tenant_branch_state_and_active_state_remain_required() -> None:
    for name in ("_branch_read_expr", "_branch_write_expr"):
        body = _function_source(name)
        assert "public.org_branches" in body
        assert "public.org_branch_state" in body
        assert "branch_data.org_id =" in body
        assert "app.current_org_id" in body
        assert "branch_state.deleted_at IS NULL" in body
        assert "branch_state.is_active IS TRUE" in body


def test_revision_changes_policies_only_and_adds_no_privilege_escape() -> None:
    source = _source().upper()
    for forbidden in (
        "GRANT ALL",
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "GRANT SELECT ON",
        "GRANT INSERT ON",
        "GRANT UPDATE ON",
        "GRANT DELETE ON",
        "CREATE ROLE",
        "ALTER ROLE",
    ):
        assert forbidden not in source


def test_downgrade_restores_member_only_b4_policy_shape() -> None:
    source = _source()
    restore = _function_source("_restore_b4_policies")
    member = _function_source("_b4_active_member_expr")
    branch = _function_source("_b4_branch_member_expr")

    assert "public.organization_members" in member
    assert "membership_status_id = 3" in member
    assert "public.branch_staff_roles" in branch
    assert "role_assignment.role_id = 3" in branch
    assert "_b4_active_member_expr" in restore
    assert "_b4_branch_member_expr" in restore
