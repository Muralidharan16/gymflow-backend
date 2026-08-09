from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import Request
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
    """Validate the runtime test URL without conflating database and identity.

    An isolated CI lane may intentionally migrate a disposable test database as
    ``migration_owner`` and exercise that *same database* as a reduced runtime
    login. That is safe only when the database is unmistakably disposable and
    the two identities are distinct. Exact URL reuse or same-user reuse remains
    forbidden.
    """
    if not test_database_url:
        raise RuntimeError("TEST_DATABASE_URL is required for pytest. Refusing to use DATABASE_URL.")

    test_url = make_url(test_database_url)
    test_db_name = test_url.database or ""
    if "test" not in test_db_name.lower():
        raise RuntimeError(f"TEST_DATABASE_URL database name must contain 'test': {test_db_name}")

    if database_url:
        app_url = make_url(database_url)
        app_db_name = app_url.database or ""

        if test_database_url == database_url:
            raise RuntimeError(
                "TEST_DATABASE_URL must use a distinct runtime identity; "
                "exact DATABASE_URL reuse is forbidden."
            )

        if test_db_name == app_db_name:
            if app_url.username == test_url.username:
                raise RuntimeError(
                    "TEST_DATABASE_URL must use a distinct runtime identity when "
                    f"sharing disposable test database {test_db_name!r}."
                )
            if "test" not in app_db_name.lower():
                raise RuntimeError(
                    f"TEST_DATABASE_URL must not point at the app database: {test_db_name}"
                )

    return test_database_url


def validate_test_admin_database_url(
    admin_database_url: str | None,
    runtime_database_url: str,
) -> str:
    """Require an explicit admin connection to the same disposable test DB.

    Application requests and repository/service tests run through the reduced
    runtime login. Destructive fixture cleanup is a separate test-harness
    capability and must never be obtained by widening the runtime role.
    """
    if not admin_database_url:
        raise RuntimeError(
            "TEST_ADMIN_DATABASE_URL is required for pytest cleanup. "
            "Refusing to grant destructive cleanup capability to the runtime identity."
        )

    runtime_url = make_url(runtime_database_url)
    admin_url = make_url(admin_database_url)
    runtime_db = runtime_url.database or ""
    admin_db = admin_url.database or ""

    if "test" not in admin_db.lower():
        raise RuntimeError(
            f"TEST_ADMIN_DATABASE_URL database name must contain 'test': {admin_db}"
        )
    if admin_db != runtime_db:
        raise RuntimeError(
            "TEST_ADMIN_DATABASE_URL must target the same disposable database "
            f"as TEST_DATABASE_URL: runtime={runtime_db!r}, admin={admin_db!r}"
        )
    if admin_database_url == runtime_database_url:
        raise RuntimeError(
            "TEST_ADMIN_DATABASE_URL must use a distinct privileged identity; "
            "runtime and cleanup connections must not be identical."
        )
    if admin_url.username == runtime_url.username:
        raise RuntimeError(
            "TEST_ADMIN_DATABASE_URL must use a distinct privileged identity; "
            "runtime and cleanup usernames must differ."
        )

    return admin_database_url


APP_DATABASE_URL = os.environ.get("DATABASE_URL") or _read_dotenv_value("DATABASE_URL")
PLATFORM_BILLING_TEST_DATABASE_URL = os.environ.get("PLATFORM_BILLING_TEST_DATABASE_URL")
FINANCE_CORE_TEST_DATABASE_URL = os.environ.get("FINANCE_CORE_TEST_DATABASE_URL")

if PLATFORM_BILLING_TEST_DATABASE_URL:
    TEST_DATABASE_URL = validate_test_database_url(
        PLATFORM_BILLING_TEST_DATABASE_URL,
        APP_DATABASE_URL,
    )
    TEST_ADMIN_DATABASE_URL = validate_test_admin_database_url(
        os.environ.get("PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL"),
        TEST_DATABASE_URL,
    )
elif FINANCE_CORE_TEST_DATABASE_URL:
    TEST_DATABASE_URL = validate_test_database_url(
        FINANCE_CORE_TEST_DATABASE_URL,
        APP_DATABASE_URL,
    )
    TEST_ADMIN_DATABASE_URL = validate_test_admin_database_url(
        os.environ.get("FINANCE_CORE_TEST_ADMIN_DATABASE_URL"),
        TEST_DATABASE_URL,
    )
else:
    TEST_DATABASE_URL = validate_test_database_url(
        os.environ.get("TEST_DATABASE_URL"),
        APP_DATABASE_URL,
    )
    TEST_ADMIN_DATABASE_URL = validate_test_admin_database_url(
        os.environ.get("TEST_ADMIN_DATABASE_URL"),
        TEST_DATABASE_URL,
    )

# Force this pytest process, including app.core.database import-time engine creation,
# onto the guarded *runtime* test identity. Administrative fixture cleanup uses a
# separate engine below and is never exposed to application dependencies.
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

admin_test_async_engine = create_async_engine(
    TEST_ADMIN_DATABASE_URL,
    poolclass=NullPool,
    pool_pre_ping=True,
    echo=False,
)

AdminTestSessionLocal = async_sessionmaker(
    admin_test_async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Keep direct legacy application-session imports from tests safe while they are
# migrated. These aliases intentionally point only at the reduced runtime pool.
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
    """Perform destructive fixture cleanup only through the admin test identity."""
    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await truncate_test_tables(session, table_names)
        await session.commit()


async def override_get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Use the disposable runtime pool while preserving production request context.

    Forced-RLS integration tests exercise the same typed principal + tenant GUCs
    that production ``get_db`` derives from ``request.state``. The test harness
    must not collapse owner/staff identities back into an untyped UUID.
    """
    principal_id = "00000000-0000-0000-0000-000000000000"
    principal_type = None
    org_id = None
    gym_id = None
    role = "unknown"
    trace_id = "test"

    state = request.state
    principal_id = getattr(
        state,
        "principal_id",
        getattr(state, "staff_id", principal_id),
    )
    principal_type = getattr(state, "principal_type", None)
    org_id = getattr(state, "org_id", None)
    gym_id = getattr(state, "gym_id", None)
    role = getattr(state, "role", "unknown")
    trace_id = (
        getattr(state, "otel_trace_id", None)
        or getattr(state, "correlation_id", "test")
    )

    async with TestSessionLocal() as session:
        try:
            await app_database.SessionContextInitializer.initialize(
                session,
                user_id=str(principal_id),
                principal_type=str(principal_type) if principal_type else None,
                org_id=str(org_id) if org_id else None,
                gym_id=str(gym_id) if gym_id else None,
                trace_id=str(trace_id),
                role=str(role),
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
    """Application-facing DB session: always the reduced runtime identity."""
    async with TestSessionLocal() as session:
        await assert_test_database(session)
        yield session


@pytest_asyncio.fixture
async def admin_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Explicit fixture/setup session; never injected into application routes."""
    async with AdminTestSessionLocal() as session:
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
