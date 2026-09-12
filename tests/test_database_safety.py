from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

import conftest as test_db


def test_validate_requires_test_database_url():
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL is required"):
        test_db.validate_test_database_url(
            None,
            "postgresql+asyncpg://postgres:pass@localhost:5432/gymflow",
        )


def test_validate_rejects_exact_application_identity_reuse():
    url = "postgresql+asyncpg://app_runtime:pass@localhost:5432/gymflow_test"
    with pytest.raises(
        RuntimeError,
        match="distinct runtime identity; exact DATABASE_URL reuse is forbidden",
    ):
        test_db.validate_test_database_url(url, url)


def test_validate_rejects_same_user_when_sharing_disposable_test_database():
    test_url = (
        "postgresql+asyncpg://app_runtime:test-pass@localhost:5432/gymflow_test"
    )
    app_url = (
        "postgresql+asyncpg://app_runtime:other-pass@localhost:5432/gymflow_test"
    )

    with pytest.raises(
        RuntimeError,
        match="distinct runtime identity when sharing disposable test database",
    ):
        test_db.validate_test_database_url(test_url, app_url)


def test_validate_allows_same_disposable_database_with_distinct_reduced_identity():
    test_url = (
        "postgresql+asyncpg://app_test_runtime:test-pass@localhost:5432/gymflow_test"
    )
    migration_url = (
        "postgresql+asyncpg://migration_owner:migration-pass@localhost:5432/gymflow_test"
    )

    assert test_db.validate_test_database_url(test_url, migration_url) == test_url


def test_validate_rejects_database_without_test_in_name():
    with pytest.raises(RuntimeError, match="must contain 'test'"):
        test_db.validate_test_database_url(
            "postgresql+asyncpg://postgres:pass@localhost:5432/gymflow",
            "postgresql+asyncpg://postgres:pass@localhost:5432/gymflow_prod",
        )


@pytest.mark.asyncio
async def test_assert_test_database_allows_current_test_db(db_session):
    db_name = await test_db.assert_test_database(db_session)
    assert "test" in db_name.lower()


@pytest.mark.asyncio
async def test_assert_test_database_rejects_non_test_db(monkeypatch, db_session):
    async def fake_execute(_statement):
        class Result:
            @staticmethod
            def scalar_one():
                return "gymflow"

        return Result()

    monkeypatch.setattr(db_session, "execute", fake_execute)

    with pytest.raises(
        RuntimeError,
        match="Refusing to run test cleanup on non-test database: gymflow",
    ):
        await test_db.assert_test_database(db_session)


def _literal_strings(node: ast.AST) -> list[str]:
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _contains_executable_fk_bypass(path: Path, forbidden: str) -> bool:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node)
        if call_name not in {
            "text",
            "execute",
            "exec_driver_sql",
            "executemany",
            "run",
            "Popen",
            "check_call",
            "check_output",
        }:
            continue
        if any(forbidden in value for value in _literal_strings(node)):
            return True
    return False


def test_tests_do_not_disable_fk_constraints():
    forbidden = "session_" + "replication_role"
    test_root = Path(__file__).resolve().parent
    offenders: list[str] = []

    for path in sorted(test_root.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        if _contains_executable_fk_bypass(path, forbidden):
            offenders.append(path.name)

    assert offenders == [], (
        "tests must not execute PostgreSQL FK/trigger bypass through "
        f"session_replication_role; offenders: {offenders}"
    )
