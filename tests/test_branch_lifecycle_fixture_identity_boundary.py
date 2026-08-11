from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("tests/test_branch_lifecycle.py")


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(_source(), filename=str(SOURCE))
    return next(
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )


def _function_source(name: str) -> str:
    source = _source()
    node = _function_node(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _executable_string_literals(name: str) -> list[str]:
    """Return string literals from executable function body, excluding docstring."""
    node = _function_node(name)
    body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
    values: list[str] = []
    for statement in body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                values.append(child.value)
    return values


def test_lifecycle_fixture_seeds_tenant_root_only_through_admin_identity() -> None:
    source = _source()
    fixture = _function_source("lifecycle_setup")

    assert "from conftest import AdminTestSessionLocal, assert_test_database" in source
    assert "async with AdminTestSessionLocal() as session:" in fixture
    assert "Organization(id=org_id" in fixture
    assert "await set_db_session_context(session, str(org_id), str(owner_id), \"owner\")" in fixture

    # The reduced production-equivalent runtime must not become a fixture
    # backdoor for tenant-root or initial branch creation.
    assert "async with AsyncSessionLocal() as session:" not in fixture


def test_lifecycle_cleanup_is_admin_only_without_cascade_or_runtime_grants() -> None:
    cleanup = _function_source("cleanup_lifecycle_fixture")
    executable_literals = _executable_string_literals("cleanup_lifecycle_fixture")

    assert "async with AdminTestSessionLocal() as session:" in cleanup
    assert "await assert_test_database(session)" in cleanup
    assert "RESET ROLE" in cleanup
    assert "TRUNCATE TABLE" in cleanup
    # Inspect executable literals rather than comments/docstrings. A truthful
    # explanation that cleanup runs "without CASCADE" must not trip the guard;
    # an actual SQL statement containing CASCADE still fails closed.
    assert all("CASCADE" not in literal.upper() for literal in executable_literals)
    assert "branch_status_history" in cleanup
    assert "branch_lifecycle_events" in cleanup
    assert "branch_outbox_events" in cleanup
    assert "branch_watchdog_alerts" in cleanup
    assert "DELETE FROM public.org_branch_state WHERE org_id = :org_id" in cleanup
    assert "DELETE FROM public.org_branches WHERE org_id = :org_id" in cleanup
    assert "DELETE FROM public.organizations WHERE id = :org_id" in cleanup


def test_lifecycle_behavior_still_executes_on_reduced_runtime() -> None:
    for test_name in (
        "test_initiate_transition_unauthorized_role",
        "test_initiate_transition_missing_reason_on_terminal",
        "test_initiate_transition_last_active_branch_guard",
        "test_saga_happy_path_and_failure",
        "test_watchdog_and_reconcile",
    ):
        test_source = _function_source(test_name)
        assert "AsyncSessionLocal()" in test_source
        assert "AdminTestSessionLocal()" not in test_source
