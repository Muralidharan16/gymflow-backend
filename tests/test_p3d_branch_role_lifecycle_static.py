from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function_source(path: str, function_name: str) -> str:
    source = _read(path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {function_name!r} not found in {path}")


def test_p3d_canonical_lifecycle_states_and_admin_compatibility_are_pinned() -> None:
    seed = _read("alembic/versions/df59095a360e_branch_lifecycle_control_plane.py")
    expected_states = {
        "active",
        "temporarily_closed",
        "under_renovation",
        "compliance_suspended",
        "permanently_closed",
    }
    for state in expected_states:
        assert state in seed

    bridge = _read("alembic/versions/708192a3b4c5_branch_runtime_privilege_boundary.py")
    assert "array_append(allowed_roles, 'admin')" in bridge
    assert "'org_admin' = ANY(allowed_roles)" in bridge
    assert "NOT ('admin' = ANY(allowed_roles))" in bridge


def test_p3d_tenant_transition_route_delegates_verified_org_and_role() -> None:
    transition = _function_source("app/routers/branch_lifecycle.py", "transition_branch")
    assert "get_current_active_staff" in transition
    assert "get_db" in transition
    assert "org_id=current_staff.org_id" in transition
    assert "actor_id=current_staff.id" in transition
    assert "actor_role=current_staff.role" in transition
    assert "BranchLifecycleService" in transition


def test_p3d_branch_reads_are_role_state_tenant_and_branch_scoped() -> None:
    router = _read("app/routers/branch_lifecycle.py")
    assert router.count("BranchAccessGuard()") >= 2

    list_route = _function_source("app/routers/branch_lifecycle.py", "list_branches")
    assert "_branch_scope_ids(current_staff)" in list_route
    assert "OrgBranch.id.in_(scoped_branch_ids)" in list_route
    assert "BranchContactORM.branch_id.in_(scoped_branch_ids)" in list_route

    deps = _read("app/core/deps.py")
    for state in (
        "active",
        "temporarily_closed",
        "under_renovation",
        "compliance_suspended",
        "permanently_closed",
    ):
        assert state in deps
    assert "OrgBranchState.org_id == staff.org_id" in deps

    guard = _function_source("app/core/deps.py", "__call__")
    assert 'role in ("manager", "trainer")' in guard
    assert "str(branch_id) not in staff.branch_ids" in guard
    assert 'role in ("manager", "trainer") and staff.gym_id is not None' not in guard


def test_p3d_global_maintenance_enqueue_is_control_plane_only() -> None:
    authorization = _function_source("app/routers/branch_lifecycle.py", "_require_maintenance_operator")
    assert 'current_staff.role not in ("superadmin", "compliance")' in authorization
    assert '"owner"' not in authorization
    assert '"admin"' not in authorization

    watchdog = _function_source("app/routers/branch_lifecycle.py", "trigger_watchdog_sweep")
    reconcile = _function_source("app/routers/branch_lifecycle.py", "trigger_reconciliation_sweep")
    for source in (watchdog, reconcile):
        assert "_require_maintenance_operator(current_staff)" in source
        assert ".delay()" in source
        assert "get_db" not in source
        assert "get_lifecycle_maintenance_db" not in source


def test_p3d_reserved_control_plane_roles_are_not_normal_owner_login_roles() -> None:
    security = _read("app/core/security.py")
    auth_service = _read("app/services/auth_service.py")

    assert 'ACCESS_TOKEN_PRINCIPAL_TYPES = frozenset({"owner", "organization_user"})' in security
    assert 'role: str = "owner"' in security
    login = _function_source("app/services/auth_service.py", "login")
    assert "create_access_token(owner.id, owner.org_id, owner.email)" in login
    assert 'role="superadmin"' not in auth_service
    assert 'role="compliance"' not in auth_service


def test_p3d_worker_and_maintenance_boundaries_preserve_force_rls_and_no_bypass() -> None:
    worker = _read("alembic/versions/2c3d4e5f6071_harden_branch_lifecycle_worker.py")
    scoped_rls = _read("alembic/versions/718293a4b5c6_scope_lifecycle_rls_policies_by_role.py")
    maintenance = _read("alembic/versions/b5c6d7e8f9a0_bound_lifecycle_maintenance_runtime.py")

    assert "FORCE ROW LEVEL SECURITY" in worker
    assert "FORCE ROW LEVEL SECURITY" in scoped_rls
    assert "FORCE ROW LEVEL SECURITY" in maintenance
    assert "NOBYPASSRLS" in maintenance
    assert "app.internal_maintenance" in maintenance
    assert "lifecycle_maintenance_runtime" in maintenance
    assert "worker_runtime" in worker


def test_p3d_transition_service_locks_tenant_bound_state_and_checks_catalog_role() -> None:
    service = _function_source("app/services/branch_lifecycle_service.py", "initiate_transition")
    assert "with_for_update()" in service
    assert "OrgBranchState.org_id == org_id" in service
    assert "actor_role not in transition.allowed_roles" in service
    assert "lifecycle_transition_in_progress" in service
    assert "status reason is required" in service.lower()
