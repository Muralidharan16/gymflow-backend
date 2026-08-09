from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


CONFTEST = Path("tests/conftest.py")
PRODUCTION_DB = Path("app/core/database.py")
AUTH_DB = Path("app/core/auth_database.py")


def _function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function {name} in {path}")


def _sync_function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name} in {path}")


def _source_segment(path: Path, node: ast.AST) -> str:
    source = path.read_text(encoding="utf-8")
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def _load_test_url_validator():
    node = _sync_function(CONFTEST, "validate_test_database_url")
    namespace = {"make_url": make_url}
    exec(_source_segment(CONFTEST, node), namespace)
    return namespace["validate_test_database_url"]


def test_pytest_db_override_requires_fastapi_request_injection() -> None:
    override = _function(CONFTEST, "override_get_db")
    assert len(override.args.args) == 1
    request_arg = override.args.args[0]
    assert request_arg.arg == "request"
    assert isinstance(request_arg.annotation, ast.Name)
    assert request_arg.annotation.id == "Request"
    assert override.args.defaults == []


def test_production_context_initializer_is_the_single_request_state_contract() -> None:
    initializer = _source_segment(
        PRODUCTION_DB,
        _function(PRODUCTION_DB, "initialize_request_session"),
    )
    ordinary = _source_segment(PRODUCTION_DB, _function(PRODUCTION_DB, "get_db"))
    auth = _source_segment(AUTH_DB, _function(AUTH_DB, "get_auth_db"))

    for token in (
        'getattr(state, "staff_id", principal_id)',
        'getattr(state, "org_id", None)',
        'getattr(state, "gym_id", None)',
        'getattr(state, "role", "unknown")',
        'getattr(state, "otel_trace_id", None)',
        'getattr(state, "correlation_id",',
        "SessionContextInitializer.initialize",
        "org_id=str(org_id) if org_id else None",
        "gym_id=str(gym_id) if gym_id else None",
    ):
        assert token in initializer

    assert "await initialize_request_session(session, request)" in ordinary
    assert "await initialize_request_session(session, request)" in auth


def test_pytest_override_preserves_same_typed_context_fields() -> None:
    production = _source_segment(
        PRODUCTION_DB,
        _function(PRODUCTION_DB, "initialize_request_session"),
    )
    override = _source_segment(CONFTEST, _function(CONFTEST, "override_get_db"))

    for token in (
        'getattr(state, "org_id", None)',
        'getattr(state, "gym_id", None)',
        'getattr(state, "role", "unknown")',
        'getattr(state, "otel_trace_id", None)',
        "principal_type",
        "SessionContextInitializer.initialize",
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


def test_test_url_validator_allows_same_disposable_db_with_distinct_identity() -> None:
    validate = _load_test_url_validator()
    runtime = (
        "postgresql+asyncpg://finance_test_runtime:runtime@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    migration = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    assert validate(runtime, migration) == runtime


def test_test_url_validator_rejects_same_identity_on_shared_disposable_db() -> None:
    validate = _load_test_url_validator()
    runtime = (
        "postgresql+asyncpg://migration_owner:runtime@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    migration = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    with pytest.raises(RuntimeError, match="distinct runtime identity"):
        validate(runtime, migration)


def test_test_url_validator_rejects_exact_database_url_reuse() -> None:
    validate = _load_test_url_validator()
    url = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_test"
    )
    with pytest.raises(RuntimeError, match="exact DATABASE_URL reuse"):
        validate(url, url)


def test_test_url_validator_still_rejects_non_test_database() -> None:
    validate = _load_test_url_validator()
    runtime = "postgresql+asyncpg://app_runtime:runtime@127.0.0.1:5432/gymflow"
    migration = "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/gymflow"
    with pytest.raises(RuntimeError, match="must contain 'test'"):
        validate(runtime, migration)
