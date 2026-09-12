from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M0006 = ROOT / "alembic/versions/0006_branch_security_audit.py"


def _upgrade_source() -> str:
    source = M0006.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(M0006))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "upgrade"
    )
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_branch_rls_policies_treat_empty_tenant_context_as_absent() -> None:
    upgrade = _upgrade_source()

    safe_expression = (
        "NULLIF(\n"
        "                current_setting('app.current_org_id', true), ''\n"
        "            )::UUID"
    )
    assert upgrade.count(safe_expression) == 3

    # A missing/cleared custom GUC must fail the RLS predicate closed; a direct
    # cast of PostgreSQL's empty placeholder raises before the policy can deny.
    assert "current_setting('app.current_org_id', true)::UUID" not in upgrade


def test_branch_rls_context_fix_does_not_disable_rls() -> None:
    upgrade = _upgrade_source()
    assert "ALTER TABLE org_branches ENABLE ROW LEVEL SECURITY;" in upgrade
    assert "ALTER TABLE org_branch_state ENABLE ROW LEVEL SECURITY;" in upgrade
    assert "ALTER TABLE branch_audit_log ENABLE ROW LEVEL SECURITY;" in upgrade
