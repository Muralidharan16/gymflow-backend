from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app/routers/branch_lifecycle.py"


def _function_source(name: str) -> str:
    source = ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing lifecycle router function: {name}")


def test_transition_http_boundary_is_tenant_and_actor_bound() -> None:
    source = _function_source("transition_branch")
    assert "Depends(get_current_active_staff)" in source
    assert "Depends(get_db)" in source
    assert "org_id=current_staff.org_id" in source
    assert "actor_id=current_staff.id" in source
    assert "actor_role=current_staff.role" in source
    assert "OrgBranchState.org_id == current_staff.org_id" in source


def test_state_and_history_http_reads_use_branch_access_guard() -> None:
    state = _function_source("get_branch_state")
    history = _function_source("get_branch_history")
    assert "Depends(BranchAccessGuard())" in state
    assert "Depends(BranchAccessGuard())" in history
    assert "OrgBranchState.org_id == current_staff.org_id" in state


def test_branch_list_applies_explicit_branch_scope_before_contact_lookup() -> None:
    source = _function_source("list_branches")
    assert "scoped_branch_ids = _branch_scope_ids(current_staff)" in source
    assert "OrgBranch.id.in_(scoped_branch_ids)" in source
    assert "BranchContactORM.branch_id.in_(scoped_branch_ids)" in source
    assert 'return {"data": []}' in source


def test_global_sweep_routes_do_not_borrow_api_or_maintenance_db_sessions() -> None:
    for function_name in ("trigger_watchdog_sweep", "trigger_reconciliation_sweep"):
        source = _function_source(function_name)
        assert "Depends(get_current_active_staff)" in source
        assert "_require_maintenance_operator(current_staff)" in source
        assert ".delay()" in source
        assert "Depends(get_db)" not in source
        assert "get_lifecycle_maintenance_db" not in source


def test_global_sweep_authority_excludes_tenant_owner_and_admin() -> None:
    source = _function_source("_require_maintenance_operator")
    assert 'current_staff.role not in ("superadmin", "compliance")' in source
    assert '"owner"' not in source
    assert '"admin"' not in source
