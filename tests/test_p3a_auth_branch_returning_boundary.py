from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c57d8e9f0a1f_p3a_auth_branch_returning_boundary.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _literal_text() -> str:
    tree = ast.parse(_source(), filename=str(MIGRATION))
    return " ".join(
        item.value
        for item in ast.walk(tree)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_c57_extends_auth_decoupling_lineage() -> None:
    source = _source()
    assert 'revision = "c57d8e9f0a1f"' in source
    assert 'down_revision = "c47d8e9f0a1e"' in source


def test_only_insert_returning_columns_are_readable_by_auth() -> None:
    source = _source()
    assert '_RETURNING_COLUMNS = ("search_normalized_name", "created_at", "updated_at")' in source
    assert '_EXPECTED_TABLE_ACL = {"INSERT", "UPDATE"}' in source
    normalized_literals = " ".join(_literal_text().split())
    grant = (
        "GRANT SELECT (search_normalized_name, created_at, updated_at) "
        "ON TABLE public.org_branches TO auth_runtime"
    )
    revoke = (
        "REVOKE SELECT (search_normalized_name, created_at, updated_at) "
        "ON TABLE public.org_branches FROM auth_runtime"
    )
    assert grant in normalized_literals
    assert revoke in normalized_literals
    assert "auth_runtime unexpectedly has broad org_branches SELECT" in source


def test_branch_returning_boundary_preserves_force_rls_and_no_broad_dml() -> None:
    source = _source()
    assert "org_branches must retain ENABLE + FORCE RLS" in source
    executable = (_function_source("upgrade") + _function_source("downgrade")).upper()
    for forbidden in (
        "GRANT SELECT ON TABLE PUBLIC.ORG_BRANCHES",
        "GRANT INSERT",
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT ALL",
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
    ):
        assert forbidden not in executable


def test_downgrade_restores_zero_auth_column_acl() -> None:
    downgrade = _function_source("downgrade")
    assert "_require_forward(bind)" in downgrade
    assert "REVOKE SELECT (search_normalized_name, created_at, updated_at)" in downgrade
    assert "_require_predecessor(bind)" in downgrade
