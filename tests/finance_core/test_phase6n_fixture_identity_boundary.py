from __future__ import annotations

import ast
from pathlib import Path


TARGET = Path("tests/finance_core/test_phase6n_sandbox_checkout_route_enablement.py")


def _source() -> str:
    return TARGET.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    module = ast.parse(_source(), filename=str(TARGET))
    node = next(
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.unparse(node)


def test_route_tenant_root_fixture_uses_guarded_admin_identity() -> None:
    source = _source()
    seed = _function_source("seed_route_organization")

    assert "from tests.finance_core.admin_database import finance_admin_session" in source
    assert "async with finance_admin_session() as session" in seed
    assert "AsyncSessionLocal" not in source
    assert "INSERT INTO organizations" in seed


def test_checkout_service_still_receives_normal_request_database_dependency() -> None:
    override = _function_source("override_checkout_dependencies")

    assert "Depends(get_db)" in override
    assert "FinanceCheckoutOrchestrationService" in override
    assert "finance_admin_session" not in override
