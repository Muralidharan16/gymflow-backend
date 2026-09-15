"""P7 graceful shutdown for resources owned by the API process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("doers.api_resources")

_UNSET = object()


async def dispose_api_database_engines(
    async_db_engine: Any,
    sync_db_engine: Any | None,
) -> tuple[Exception, ...]:
    """Dispose API database pools independently and without broadening ownership.

    Worker and maintenance engines are intentionally not accepted or discovered by
    this helper.  Cleanup is best-effort so one failing disposer cannot prevent the
    other API-owned pool from being closed during process termination.
    """
    errors: list[Exception] = []

    try:
        await async_db_engine.dispose()
    except Exception as exc:  # pragma: no cover - exercised with fault fixture
        logger.exception("Error disposing API async database engine")
        errors.append(exc)

    if sync_db_engine is not None:
        try:
            await asyncio.to_thread(sync_db_engine.dispose)
        except Exception as exc:  # pragma: no cover - exercised with fault fixture
            logger.exception("Error disposing API sync database engine")
            errors.append(exc)

    return tuple(errors)


async def close_api_runtime_resources(
    *,
    redis_closer: Callable[[], Awaitable[None]] | None = None,
    async_db_engine: Any = _UNSET,
    sync_db_engine: Any = _UNSET,
) -> tuple[Exception, ...]:
    """Close Redis and API database resources after request/background drain.

    Optional arguments exist for deterministic P7 fault tests. Production calls use
    the lazily imported process-owned singletons.  The function is safe to call more
    than once because Redis close and SQLAlchemy engine disposal are idempotent.
    """
    if redis_closer is None:
        from app.core.redis import close_redis

        redis_closer = close_redis

    if async_db_engine is _UNSET or sync_db_engine is _UNSET:
        from app.core.database import async_engine as api_async_engine
        from app.core.database import sync_engine as api_sync_engine

        if async_db_engine is _UNSET:
            async_db_engine = api_async_engine
        if sync_db_engine is _UNSET:
            sync_db_engine = api_sync_engine

    errors: list[Exception] = []
    try:
        await redis_closer()
    except Exception as exc:  # close_redis currently swallows, retain hard boundary
        logger.exception("Error closing API Redis resources")
        errors.append(exc)

    errors.extend(
        await dispose_api_database_engines(
            async_db_engine,
            sync_db_engine,
        )
    )
    return tuple(errors)
