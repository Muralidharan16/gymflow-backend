from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("app/services/branch_lifecycle_service.py")
SERVICE_CLASS = "BranchLifecycleService"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _method_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(_source(), filename=str(SOURCE))
    classes = [
        item
        for item in module.body
        if isinstance(item, ast.ClassDef) and item.name == SERVICE_CLASS
    ]
    assert len(classes) == 1, (
        f"expected exactly one {SERVICE_CLASS} class, found {len(classes)}"
    )
    matches = [
        item
        for item in classes[0].body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {SERVICE_CLASS}.{name} method, found {len(matches)}"
    )
    return matches[0]


def _method_source(name: str) -> str:
    """Return one concrete BranchLifecycleService method from source."""
    source = _source()
    node = _method_node(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _contains_for_update(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child) == "with_for_update"
        for child in ast.walk(node)
    )


def _contains_org_branch_state_select(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or _call_name(child) != "select":
            continue
        if any(
            isinstance(arg, ast.Name) and arg.id == "OrgBranchState"
            for arg in child.args
        ):
            return True
    return False


def _literal_strings(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def test_transition_authorizes_from_plain_read_before_write_locking() -> None:
    node = _method_node("initiate_transition")
    transition = _method_source("initiate_transition")

    plain_reads = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Assign)
        and _contains_org_branch_state_select(child.value)
        and not _contains_for_update(child.value)
    ]
    assert plain_reads, "transition must perform a tenant-visible plain state read"
    visible_read_line = min(child.lineno for child in plain_reads)

    authorization_checks = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.If)
        and "actor_role" in ast.unparse(child.test)
        and "allowed_roles" in ast.unparse(child.test)
    ]
    assert len(authorization_checks) == 1
    authorization_line = authorization_checks[0].lineno

    advisory_lock_calls = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and any(
            "pg_catalog.pg_advisory_xact_lock" in value
            for value in _literal_strings(child)
        )
    ]
    assert len(advisory_lock_calls) >= 2, (
        "organization and branch advisory locks must both remain present"
    )
    advisory_lock_line = min(child.lineno for child in advisory_lock_calls)

    write_locks = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _call_name(child) == "with_for_update"
    ]
    assert len(write_locks) == 1
    write_lock_line = write_locks[0].lineno

    assert visible_read_line < authorization_line < advisory_lock_line < write_lock_line
    assert "status_code=status.HTTP_403_FORBIDDEN" in transition
    assert "branch_state.status != from_status" in transition
    assert "status_code=status.HTTP_409_CONFLICT" in transition
    assert "Branch status changed concurrently" in transition


def test_optional_booking_surface_is_checked_before_update_without_swallowing_errors() -> None:
    relation_exists = _method_source("_relation_exists")
    cancel = _method_source("_cancel_future_bookings_if_present")
    saga = _method_source("execute_saga_cascade")

    assert "pg_catalog.to_regclass(:qualified_name) IS NOT NULL" in relation_exists
    assert 'if not await self._relation_exists("public.bookings")' in cancel
    assert "UPDATE public.bookings" in cancel

    # Missing optional infrastructure is handled before DML. Once the table is
    # present, SQL/ACL/data failures are real saga failures and must reach the
    # outer rollback+compensation path instead of being hidden inside a nested
    # broad catch that leaves PostgreSQL in failed-transaction state.
    assert "except Exception" not in cancel
    assert "_cancel_future_bookings_if_present" in saga
    assert "await self.db.rollback()" in saga
    assert "await self._compensate_saga" in saga


def test_reconciliation_isolates_per_branch_failures_with_savepoints() -> None:
    reconcile = _method_source("run_reconciliation_sweep")

    assert "async with self.db.begin_nested():" in reconcile
    assert "update_result.rowcount != 1" in reconcile
    assert "clear_result.rowcount != 1" in reconcile
    assert "search_sync_failed_at = :now" in reconcile
    assert "return synced_count" in reconcile
    assert "return len(claimed_ids)" not in reconcile


def test_watchdog_refuses_missing_transition_timestamp_instead_of_crashing_math() -> None:
    watchdog = _method_source("run_watchdog_sweep")

    assert "if changed_at is None:" in watchdog
    assert "manual review required" in watchdog
    assert "continue" in watchdog
