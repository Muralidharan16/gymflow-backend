from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


PLATFORM_BILLING_TEST_DATABASE_URL = "PLATFORM_BILLING_TEST_DATABASE_URL"
PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL = "PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL"
PROTECTED_D11_ORGANIZATION_SLUG = "doers-razorpay-test-smoke"
PROTECTED_D11_IDEMPOTENCY_KEY = "organization-create:synthetic:test:finance-razorpay-smoke"
APPROVED_ADMIN_USER = "migration_owner"

_FORBIDDEN_DATABASES = {"gymflow_test", "gymflow_migration_test", "postgres", "template0", "template1"}
_REQUIRED_DATABASE_PREFIX = "gymflow_platform_billing_test"


class PlatformBillingTestIsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformBillingDatabaseIdentity:
    host: str | None
    port: int | None
    database: str


@dataclass(frozen=True)
class PlatformBillingTestDatabaseConfig:
    runtime_url: URL
    admin_url: URL
    identity: PlatformBillingDatabaseIdentity
    runtime_user: str | None
    admin_user: str | None

    @property
    def sanitized_identity(self) -> dict[str, object]:
        return {
            "host": self.identity.host,
            "port": self.identity.port,
            "database": self.identity.database,
            "runtime_user": self.runtime_user,
            "admin_user": self.admin_user,
        }


def get_platform_billing_test_config(
    env: Mapping[str, str | None] | None = None,
) -> PlatformBillingTestDatabaseConfig:
    values = env if env is not None else os.environ
    runtime_raw = (values.get(PLATFORM_BILLING_TEST_DATABASE_URL) or "").strip()
    admin_raw = (values.get(PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL) or "").strip()
    shared_raw = (values.get("TEST_DATABASE_URL") or "").strip()

    if not runtime_raw:
        raise _error("missing runtime Platform Billing database URL")
    if not admin_raw:
        raise _error("missing admin Platform Billing database URL")

    runtime_url = _parse_url(runtime_raw, "runtime")
    admin_url = _parse_url(admin_raw, "admin")
    shared_url = _parse_url(shared_raw, "shared") if shared_raw else None

    if shared_url is not None:
        if _url_without_password(runtime_url) == _url_without_password(shared_url):
            raise _error("runtime Platform Billing URL must not target TEST_DATABASE_URL")
        if _url_without_password(admin_url) == _url_without_password(shared_url):
            raise _error("admin Platform Billing URL must not target TEST_DATABASE_URL")

    runtime_identity = _identity(runtime_url)
    admin_identity = _identity(admin_url)
    if runtime_identity != admin_identity:
        raise _error("runtime and admin Platform Billing URLs must target the same database")

    database_name = runtime_identity.database.lower()
    if database_name in _FORBIDDEN_DATABASES:
        raise _error("Platform Billing database is not disposable")
    if not database_name.startswith(_REQUIRED_DATABASE_PREFIX):
        raise _error("Platform Billing database name must be explicitly disposable")

    if shared_url is not None:
        shared_identity = _identity(shared_url)
        if runtime_identity == shared_identity:
            raise _error("runtime Platform Billing URL must not target TEST_DATABASE_URL")
        if admin_identity == shared_identity:
            raise _error("admin Platform Billing URL must not target TEST_DATABASE_URL")

    runtime_user = runtime_url.username
    admin_user = admin_url.username
    if runtime_user and admin_user and runtime_user == admin_user:
        raise _error("runtime and admin Platform Billing users must be separated")

    return PlatformBillingTestDatabaseConfig(
        runtime_url=runtime_url,
        admin_url=admin_url,
        identity=runtime_identity,
        runtime_user=runtime_user,
        admin_user=admin_user,
    )


def create_platform_billing_admin_sessionmaker(
    config: PlatformBillingTestDatabaseConfig | None = None,
) -> tuple[object, async_sessionmaker[AsyncSession]]:
    selected = config or get_platform_billing_test_config()
    engine = create_async_engine(
        selected.admin_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def assert_platform_billing_admin_posture(
    session: AsyncSession,
    config: PlatformBillingTestDatabaseConfig,
) -> None:
    current_database = await _scalar(session, "SELECT current_database()")
    current_user = await _scalar(session, "SELECT current_user")
    if current_database != config.identity.database:
        raise _error("admin connection reached an unexpected database")
    if current_user != APPROVED_ADMIN_USER:
        raise _error("admin connection is not using the approved migration owner")


async def assert_platform_billing_runtime_posture(
    session: AsyncSession,
    config: PlatformBillingTestDatabaseConfig,
) -> None:
    current_database = await _scalar(session, "SELECT current_database()")
    if current_database != config.identity.database:
        raise _error("runtime connection reached an unexpected database")

    is_superuser = await _scalar(
        session,
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user",
    )
    if is_superuser:
        raise _error("runtime Platform Billing connection must not be superuser")

    owns_database = await _scalar(
        session,
        """
        SELECT pg_catalog.pg_get_userbyid(datdba) = current_user
        FROM pg_database
        WHERE datname = current_database()
        """,
    )
    if owns_database:
        raise _error("runtime Platform Billing connection must not own the database")


async def assert_no_protected_d11_identity(session: AsyncSession) -> None:
    organization_count = await _optional_table_count(
        session,
        "organizations",
        "slug = :slug",
        {"slug": PROTECTED_D11_ORGANIZATION_SLUG},
    )
    evidence_count = await _optional_table_count(
        session,
        "organization_creation_idempotency",
        "idempotency_key = :key",
        {"key": PROTECTED_D11_IDEMPOTENCY_KEY},
    )
    if organization_count or evidence_count:
        raise _error("protected D11 identity must not exist in Platform Billing database")


def sanitized_error_message(exc: BaseException) -> str:
    return str(exc)


def _url_without_password(url: URL) -> str:
    return url.render_as_string(hide_password=True)


def _parse_url(raw: str, label: str) -> URL:
    try:
        return make_url(raw)
    except Exception as exc:  # pragma: no cover - defensive SQLAlchemy boundary
        raise _error(f"{label} Platform Billing database URL is invalid") from exc


def _identity(url: URL) -> PlatformBillingDatabaseIdentity:
    database = (url.database or "").strip()
    if not database:
        raise _error("Platform Billing database URL must include a database name")
    return PlatformBillingDatabaseIdentity(host=url.host, port=url.port, database=database)


def _error(message: str) -> PlatformBillingTestIsolationError:
    return PlatformBillingTestIsolationError(message)


async def _scalar(session: AsyncSession, sql: str) -> object:
    result = await session.execute(text(sql))
    return result.scalar_one()


async def _table_exists(session: AsyncSession, table_name: str) -> bool:
    result = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = :table_name
            )
            """
        ),
        {"table_name": table_name},
    )
    return bool(result.scalar_one())


async def _optional_table_count(
    session: AsyncSession,
    table_name: str,
    predicate: str,
    params: dict[str, object],
) -> int:
    if not await _table_exists(session, table_name):
        return 0
    result = await session.execute(
        text(f'SELECT COUNT(*) FROM "{table_name}" WHERE {predicate}'),
        params,
    )
    return int(result.scalar_one())
