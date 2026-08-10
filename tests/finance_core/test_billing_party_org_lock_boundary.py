from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY = Path("app/finance_core/repositories/billing_parties.py")


def _source() -> str:
    return REPOSITORY.read_text(encoding="utf-8")


def _class_function(name: str) -> ast.AsyncFunctionDef:
    module = ast.parse(_source(), filename=str(REPOSITORY))
    class_node = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef)
        and node.name == "FinanceBillingPartyRepository"
    )
    return next(
        item
        for item in class_node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == name
    )


def _function_source(name: str) -> str:
    source = _source()
    node = _class_function(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_organization_serialization_uses_advisory_lock_not_row_update_lock() -> None:
    get_org = _function_source("get_organization")
    lock = _function_source("acquire_organization_creation_lock")

    assert "if for_update:" in get_org
    assert "await self.acquire_organization_creation_lock(organization_id)" in get_org
    assert "select(Organization)" in get_org

    assert "pg_advisory_xact_lock" in lock
    assert "finance:billing_party:" in lock

    # Inspect executable AST instead of matching comments/docstrings mentioning
    # the SQL phrase. The tenant-root read itself must not invoke SQLAlchemy's
    # row-level FOR UPDATE API.
    get_org_node = _class_function("get_organization")
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
        for node in ast.walk(get_org_node)
    )


def test_finance_repository_does_not_require_tenant_root_update_capability() -> None:
    get_org_node = _class_function("get_organization")

    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "UPDATE organizations" in node.value
        for node in ast.walk(get_org_node)
    )
