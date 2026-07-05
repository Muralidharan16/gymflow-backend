from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

import conftest as test_db


def test_validate_requires_test_database_url():
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL is required"):
        test_db.validate_test_database_url(None, "postgresql+asyncpg://postgres:pass@localhost:5432/gymflow")


def test_validate_rejects_same_database_name():
    url = "postgresql+asyncpg://postgres:pass@localhost:5432/gymflow_test"
    with pytest.raises(RuntimeError, match="must not point at the app database"):
        test_db.validate_test_database_url(url, url)


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

    with pytest.raises(RuntimeError, match="Refusing to run test cleanup on non-test database: gymflow"):
        await test_db.assert_test_database(db_session)


def test_tests_do_not_disable_fk_constraints():
    forbidden = "session_" + "replication_role"
    test_root = Path(__file__).resolve().parent
    offenders: list[str] = []

    for path in test_root.glob("*.py"):
        if path.name == Path(__file__).name:
            continue
        if forbidden in path.read_text():
            offenders.append(path.name)

    assert offenders == []
