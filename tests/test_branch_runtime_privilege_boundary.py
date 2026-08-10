from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/708192a3b4c5_branch_runtime_privilege_boundary.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    module = ast.parse(_source(), filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.unparse(node)


def test_branch_runtime_acl_is_operation_scoped() -> None:
    source = _source()
    upgrade = _function_source("_grant_contract")

    assert (
        "GRANT SELECT ON TABLE public.org_branches TO app_runtime"
        in upgrade
    )
    assert (
        "GRANT SELECT, UPDATE ON TABLE public.org_branch_state TO app_runtime"
        in upgrade
    )
    assert (
        "GRANT INSERT, UPDATE ON TABLE public.org_branches TO auth_runtime"
        in upgrade
    )
    assert (
        "GRANT INSERT ON TABLE public.org_branch_state TO auth_runtime"
        in upgrade
    )

    assert "GRANT ALL" not in source.upper()
    assert "BYPASSRLS" not in upgrade.upper()
    assert "ALTER ROLE" not in source
    assert "OWNER TO app_runtime" not in source
    assert "OWNER TO auth_runtime" not in source


def test_branch_runtime_acl_keeps_destructive_capabilities_forbidden() -> None:
    source = _source()

    assert '_FORBIDDEN_PRIVILEGES = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}' in source
    assert "must not have CREATE on public schema" in source
    assert "_require_no_public_dml(bind)" in source
    assert "acl.grantee = 0" in source


def test_branch_runtime_requires_forced_rls_and_reduced_roles() -> None:
    source = _source()

    assert "must retain ENABLE + FORCE ROW LEVEL SECURITY" in source
    assert "rolbypassrls" in source
    assert "NOLOGIN/NOINHERIT/NOBYPASSRLS" in source
    assert "branch runtime migration requires migration_owner" in source


def test_branch_runtime_downgrade_restores_predecessor_acl() -> None:
    downgrade = _function_source("downgrade")
    revoke = _function_source("_revoke_contract")

    assert "_verify_final_acl(bind)" in downgrade
    assert "_revoke_contract()" in downgrade
    assert "_require_predecessor_acl(bind)" in downgrade

    assert (
        "REVOKE SELECT ON TABLE public.org_branches FROM app_runtime"
        in revoke
    )
    assert (
        "REVOKE SELECT, UPDATE ON TABLE public.org_branch_state FROM app_runtime"
        in revoke
    )
    assert (
        "REVOKE INSERT, UPDATE ON TABLE public.org_branches FROM auth_runtime"
        in revoke
    )
    assert (
        "REVOKE INSERT ON TABLE public.org_branch_state FROM auth_runtime"
        in revoke
    )
