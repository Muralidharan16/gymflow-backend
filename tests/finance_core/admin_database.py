from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _required_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for Finance Core integration tests")
    return value


def _validated_urls() -> tuple[str, str]:
    runtime_url = _required_url("FINANCE_CORE_TEST_DATABASE_URL")
    admin_url = _required_url("FINANCE_CORE_TEST_ADMIN_DATABASE_URL")

    runtime = make_url(runtime_url)
    admin = make_url(admin_url)
    runtime_db = runtime.database or ""
    admin_db = admin.database or ""

    if "test" not in runtime_db.lower() or "test" not in admin_db.lower():
        raise RuntimeError(
            "Finance Core test URLs must target disposable databases whose names contain 'test'"
        )
    if runtime_db != admin_db:
        raise RuntimeError(
            "Finance Core runtime/admin URLs must target the same disposable database"
        )
    if runtime_url == admin_url or runtime.username == admin.username:
        raise RuntimeError(
            "Finance Core admin cleanup must use a distinct database identity"
        )

    return runtime_url, admin_url


@asynccontextmanager
async def finance_admin_session() -> AsyncIterator[AsyncSession]:
    """Yield a guarded, short-lived test-admin session.

    This helper is test-only. Application/service code must never import it.
    Destructive fixture operations are deliberately kept off the Finance Core
    runtime identity so runtime privilege proofs remain meaningful.
    """

    _runtime_url, admin_url = _validated_urls()
    engine = create_async_engine(
        admin_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        async with session_factory() as session:
            db_name = (
                await session.execute(text("SELECT current_database()"))
            ).scalar_one()
            if "test" not in str(db_name).lower():
                raise RuntimeError(
                    f"Refusing Finance Core admin operation on non-test database: {db_name}"
                )
            yield session
    finally:
        await engine.dispose()
