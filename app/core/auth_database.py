"""Dedicated database identity for authentication and tenant bootstrap.

The auth/bootstrap surface can create tenant roots and rotate durable auth
sessions, so it must not borrow the ordinary FastAPI database login.  This
module owns a separate SQLAlchemy pool while reusing the canonical request
Session context from :mod:`app.core.database`.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import initialize_request_session

logger = logging.getLogger("doers.auth_database")


def _validated_auth_database_url() -> str | None:
    raw = settings.AUTH_DATABASE_URL.strip()
    if not raw:
        return None

    ordinary = make_url(settings.DATABASE_URL)
    auth = make_url(raw)
    if ordinary.database != auth.database:
        raise RuntimeError(
            "AUTH_DATABASE_URL must target the same application database as DATABASE_URL"
        )
    if ordinary.username == auth.username:
        raise RuntimeError(
            "AUTH_DATABASE_URL must use a distinct PostgreSQL login from DATABASE_URL"
        )
    return raw


_AUTH_DATABASE_URL = _validated_auth_database_url()
auth_async_engine = (
    create_async_engine(
        _AUTH_DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=settings.ENVIRONMENT == "development",
    )
    if _AUTH_DATABASE_URL
    else None
)

AuthSessionLocal = (
    async_sessionmaker(
        auth_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    if auth_async_engine is not None
    else None
)


async def get_auth_db(request=None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[misc]
    """Yield the bounded auth/bootstrap session.

    Missing configuration fails closed at request time outside production. In
    production the Settings validator prevents startup without the dedicated URL.
    """
    if AuthSessionLocal is None:
        raise RuntimeError(
            "AUTH_DATABASE_URL is required for authentication/bootstrap database access"
        )

    from app.core.concurrency import adaptive_controller

    async with AuthSessionLocal() as session:
        started = time.monotonic()
        try:
            await initialize_request_session(session, request)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await adaptive_controller.record_latency(
                (time.monotonic() - started) * 1000
            )
