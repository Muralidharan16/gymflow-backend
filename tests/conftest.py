from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _read_dotenv_value(key: str) -> str | None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        if raw_key.strip() == key:
            return raw_value.strip().strip('"').strip("'")
    return None


def database_name(database_url: str) -> str:
    return make_url(database_url).database or ""


def validate_test_database_url(test_database_url: str | None, database_url: str | None) -> str:
    if not test_database_url:
        raise RuntimeError("TEST_DATABASE_URL is required for pytest. Refusing to use DATABASE_URL.")

    test_url = make_url(test_database_url)
    test_db_name = test_url.database or ""
    if "test" not in test_db_name.lower():
        raise RuntimeError(f"TEST_DATABASE_URL database name must contain 'test': {test_db_name}")

    if database_url:
        app_url = make_url(database_url)
        app_db_name = app_url.database or ""
        if test_database_url == database_url or test_db_name == app_db_name:
            raise RuntimeError(
                f"TEST_DATABASE_URL must not point at the app database: {test_db_name}"
            )

    return test_database_url


APP_DATABASE_URL = os.environ.get("DATABASE_URL") or _read_dotenv_value("DATABASE_URL")
PLATFORM_BILLING_TEST_DATABASE_URL = os.environ.get("PLATFORM_BILLING_TEST_DATABASE_URL")
if PLATFORM_BILLING_TEST_DATABASE_URL:
    TEST_DATABASE_URL = validate_test_database_url(PLATFORM_BILLING_TEST_DATABASE_URL, APP_DATABASE_URL)
else:
    TEST_DATABASE_URL = validate_test_database_url(os.environ.get("TEST_DATABASE_URL"), APP_DATABASE_URL)

# Force this pytest process, including app.core.database import-time engine creation,
# onto the guarded test database. Platform Billing can opt into a dedicated
# disposable runtime DB before app.core.database is imported.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.core import database as app_database  # noqa: E402
from app.core.pool_manager import pool_manager  # noqa: E402


test_async_engine = create_async_engine(
    TEST_DATABASE_URL,
    poolclass=NullPool,
    pool_pre_ping=True,
    echo=False,
)

TestSessionLocal = async_sessionmaker(
    test_async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Keep direct legacy imports from tests safe while they are migrated.
app_database.async_engine = test_async_engine
app_database.AsyncSessionLocal = TestSessionLocal
app_database.async_session_maker = TestSessionLocal
pool_manager.set_initial_pool(test_async_engine, TestSessionLocal)

from app.main import app  # noqa: E402


async def assert_test_database(session: AsyncSession) -> str:
    result = await session.execute(text("SELECT current_database()"))
    db_name = result.scalar_one()
    if "test" not in db_name.lower():
        raise RuntimeError(f"Refusing to run test cleanup on non-test database: {db_name}")
    return db_name


async def truncate_test_tables(session: AsyncSession, table_names: list[str]) -> None:
    await assert_test_database(session)
    if not table_names:
        return
    result = await session.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY(:table_names)
            """
        ),
        {"table_names": table_names},
    )
    existing_tables = {row[0] for row in result}
    ordered_tables = [table for table in table_names if table in existing_tables]
    if not ordered_tables:
        return
    quoted_tables = ", ".join(f'"{table}"' for table in ordered_tables)
    await session.execute(text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"))


async def cleanup_test_database_tables(table_names: list[str]) -> None:
    async with TestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await truncate_test_tables(session, table_names)
        await session.commit()


async def override_get_db(request=None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[misc]
    async with TestSessionLocal() as session:
        try:
            await app_database.SessionContextInitializer.initialize(
                session,
                user_id="00000000-0000-0000-0000-000000000000",
                org_id=None,
                gym_id=None,
                trace_id="test",
                role="unknown",
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[app_database.get_db] = override_get_db


@pytest.fixture(scope="session", autouse=True)
def require_test_database_url() -> str:
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        await assert_test_database(session)
        yield session


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session", autouse=True)
def cleanup_dependency_overrides_at_end():
    yield
    app.dependency_overrides.pop(app_database.get_db, None)
