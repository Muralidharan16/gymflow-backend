"""Service-owned API database session boundary.

P3C needs exactly one application owner for the profile + registration business
transaction.  The normal ``get_db`` dependency intentionally auto-commits at
request completion and remains unchanged for all existing routes.  This sibling
dependency keeps the same pool, verified request context, latency accounting and
rollback safety, but deliberately performs no success-path commit: the service
using it must own an explicit SQLAlchemy transaction.
"""

from __future__ import annotations

import time
from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, initialize_request_session
from app.core.pool_manager import pool_manager


async def get_service_managed_db(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-context session whose successful commit is service-owned.

    BaseException is intentional: cancellation must not leave an open P3C
    transaction on a pooled connection.  On success this dependency never calls
    ``commit``; the atomic service's ``session.begin()`` context is the sole
    business-transaction owner.
    """

    from app.core.concurrency import adaptive_controller

    active_sessionmaker = pool_manager.current_sessionmaker or AsyncSessionLocal
    async with active_sessionmaker() as session:
        t0 = time.monotonic()
        try:
            await initialize_request_session(session, request)
            yield session
        except BaseException:
            if session.in_transaction():
                await session.rollback()
            raise
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            await adaptive_controller.record_latency(elapsed_ms)
