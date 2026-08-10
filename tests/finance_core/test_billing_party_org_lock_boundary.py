from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY = Path("app/finance_core/repositories/billing_parties.py")


def _source() -> str:
    return REPOSITORY.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    module = ast.parse(source, filename=str(REPOSITORY))
    class_node = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef)
        and node.name == "FinanceBillingPartyRepository"
    )
    node = next(
        item
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_organization_serialization_uses_advisory_lock_not_row_update_lock() -> None:
    get_org = _function_source("get_organization")
    lock = _function_source("acquire_organization_creation_lock")

    assert "if for_update:" in get_org
    assert "await self.acquire_organization_creation_lock(organization_id)" in get_org
    assert ".with_for_update()" not in get_org
    assert "select(Organization)" in get_org

    assert "pg_advisory_xact_lock" in lock
    assert "finance:billing_party:" in lock


def test_finance_repository_does_not_require_tenant_root_update_capability() -> None:
    get_org = _function_source("get_organization")

    assert "UPDATE organizations" not in get_org
    assert "FOR UPDATE" not in get_org.upper()
