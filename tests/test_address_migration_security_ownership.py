from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M00F = ROOT / "alembic/versions/00f277c748ea_add_hyperscale_branch_name_and_address_.py"
M6F = ROOT / "alembic/versions/6f708192a3b4_address_runtime_privilege_boundary.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_00f_revokes_public_view_acl_before_dropping_view() -> None:
    downgrade = _function_source(M00F, "downgrade")
    revoke = 'REVOKE SELECT ON v_public_branch_addresses FROM branch_viewer;'
    drop = 'DROP VIEW v_public_branch_addresses;'

    assert downgrade.count(revoke) == 1
    assert downgrade.count(drop) == 1
    assert downgrade.index(revoke) < downgrade.index(drop)
    assert "IF EXISTS" not in downgrade[downngrade_index(downgrade, revoke):downngrade_index(downgrade, drop) + len(drop)]


def downngrade_index(source: str, token: str) -> int:
    # Small helper keeps the ordering assertion readable while preserving exact
    # source matching; the misspelling is local and has no production surface.
    return source.index(token)


def test_6f_requires_but_never_toggles_00f_owned_rls() -> None:
    predecessor = _function_source(M6F, "_require_predecessor")
    upgrade = _function_source(M6F, "upgrade")
    downgrade = _function_source(M6F, "downgrade")

    assert "_require_rls_flags(bind, enabled=True)" in predecessor
    assert "enabled=False" not in predecessor

    for function_source in (upgrade, downgrade):
        assert "ENABLE ROW LEVEL SECURITY" not in function_source
        assert "DISABLE ROW LEVEL SECURITY" not in function_source
        assert "NO FORCE ROW LEVEL SECURITY" not in function_source


def test_6f_downgrade_restores_runtime_boundary_without_rls_gap() -> None:
    downgrade = _function_source(M6F, "downgrade")

    assert "_repoint_triggers(bind, hardened=False)" in downgrade
    assert "_drop_functions(bind)" in downgrade
    assert "DROP POLICY tenant_isolation_audit_insert" in downgrade
    assert "REVOKE SELECT, INSERT, UPDATE ON TABLE public.organization_addresses FROM app_runtime" in downgrade
    assert downgrade.rstrip().endswith("_require_predecessor(bind)")
