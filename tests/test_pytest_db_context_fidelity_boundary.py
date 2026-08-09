from __future__ import annotations

import ast
from pathlib import Path


CONFTEST = Path("tests/conftest.py")
PRODUCTION_DB = Path("app/core/database.py")


def _function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function {name} in {path}")


def _source_segment(path: Path, node: ast.AST) -> str:
    source = path.read_text(encoding="utf-8")
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def test_pytest_db_override_preserves_production_request_state_context() -> None:
    production = _source_segment(PRODUCTION_DB, _function(PRODUCTION_DB, "get_db"))
    override = _source_segment(CONFTEST, _function(CONFTEST, "override_get_db"))

    for token in (
        'getattr(state, "staff_id", user_id)',
        'getattr(state, "org_id", None)',
        'getattr(state, "gym_id", None)',
        'getattr(state, "role", "unknown")',
        'getattr(state, "otel_trace_id", None)',
        'getattr(state, "correlation_id",',
        "SessionContextInitializer.initialize",
        "org_id=str(org_id) if org_id else None",
        "gym_id=str(gym_id) if gym_id else None",
    ):
        assert token in production
        assert token in override


def test_pytest_db_override_does_not_bypass_rls_or_impersonate_privileged_roles() -> None:
    override = _source_segment(CONFTEST, _function(CONFTEST, "override_get_db")).upper()

    forbidden = (
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "SET ROLE APP_RLS_EXECUTOR",
        "SET ROLE APP_SECURITY_OWNER",
        "SET SESSION AUTHORIZATION",
        "ROW_SECURITY = OFF",
    )
    for token in forbidden:
        assert token not in override
