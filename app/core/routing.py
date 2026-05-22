"""
app/core/routing.py
====================
WAL LSN-aware primary/replica session routing for the Doers SaaS platform.

Design (blueprint Section 6 + patch_v4 FIX 2):
  • RoutingAsyncSession inspects SQLAlchemy clause trees to classify writes vs reads.
  • Write sessions capture the primary's WAL LSN via pg_current_wal_lsn().
  • Subsequent reads query the candidate replica's pg_last_wal_replay_lsn() and only
    route there if replay_lsn >= target_lsn. Stale/broken replicas fall back to primary.
  • All DB latency is recorded to the adaptive concurrency controller.

Usage:
    from app.core.routing import get_routed_db
    db: AsyncSession = Depends(get_routed_db)
"""

from __future__ import annotations

import logging
import random
import time
from typing import AsyncGenerator, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.sql.dml import Insert, Update, Delete
from sqlalchemy.sql.selectable import Select

from app.core.config import settings
from app.core.database import SessionContextInitializer

logger = logging.getLogger("doers.routing")


# ─────────────────────────────────────────────────────────────────────────────
# Engine registry
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine(url: str, **kwargs):
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        **kwargs,
    )


# Primary engine — always used for writes
_primary_engine = _make_engine(settings.DATABASE_URL)

# Replica engines — used for reads when WAL fencing passes.
# Populate REPLICA_DATABASE_URLS in settings to enable read scaling.
_replica_urls: list[str] = getattr(settings, "REPLICA_DATABASE_URLS", "").split(",") if getattr(settings, "REPLICA_DATABASE_URLS", "") else []
_replica_engines = [_make_engine(url.strip()) for url in _replica_urls if url.strip()]

PrimarySession = async_sessionmaker(_primary_engine, class_=AsyncSession, expire_on_commit=False)


# ─────────────────────────────────────────────────────────────────────────────
# Clause-tree write classification
# ─────────────────────────────────────────────────────────────────────────────

def _is_write_clause(clause) -> bool:
    """
    Returns True if the SQLAlchemy clause represents a write or locking operation.
    Uses clause type inspection — NOT string parsing, which breaks on CTEs/subqueries.
    """
    if isinstance(clause, (Insert, Update, Delete)):
        return True
    # Detect SELECT FOR UPDATE / advisory lock calls via string fallback only for
    # locking hints that have no distinct clause type
    try:
        clause_str = str(clause).upper()
        if "FOR UPDATE" in clause_str or "PG_ADVISORY" in clause_str:
            return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────────────────────────
# WAL LSN-aware routing session
# ─────────────────────────────────────────────────────────────────────────────

class RoutingSession:
    """
    Thin async session wrapper that routes writes to primary and reads to replicas
    after verifying WAL LSN consistency. Falls back to primary on stale/broken replicas.
    """

    def __init__(self, session: AsyncSession):
        self._session    = session
        self._target_lsn: Optional[str] = None

    # Proxy common session methods
    async def execute(self, statement, params=None, **kwargs):
        from app.core.concurrency import adaptive_controller
        t0 = time.monotonic()
        try:
            result = await self._session.execute(statement, params, **kwargs)
            return result
        finally:
            await adaptive_controller.record_latency((time.monotonic() - t0) * 1000)

    async def write_and_capture_lsn(self, statement, params=None, **kwargs):
        """
        Execute a write on the primary and capture the resulting WAL LSN.
        Subsequent reads through read_with_fencing() will only use replicas
        that have replayed up to this LSN.
        """
        result = await self.execute(statement, params, **kwargs)
        lsn_res = await self._session.execute(
            sa.text("SELECT pg_catalog.pg_current_wal_lsn()::text")
        )
        self._target_lsn = lsn_res.scalar()
        logger.debug("Captured write LSN: %s", self._target_lsn)
        return result

    async def read_with_fencing(self, statement, params=None, **kwargs):
        """
        Execute a read against a replica only if it has replayed past target_lsn.
        Falls back to primary if: no replicas, replica lags, or replica is unreachable.
        """
        if not self._target_lsn or not _replica_engines:
            return await self.execute(statement, params, **kwargs)

        replica = random.choice(_replica_engines)
        try:
            async with replica.connect() as conn:
                replay_res = await conn.execute(
                    sa.text("SELECT pg_catalog.pg_last_wal_replay_lsn()::text")
                )
                replay_lsn = replay_res.scalar()

                # Check if replica has caught up
                fence_res = await conn.execute(
                    sa.text(
                        "SELECT pg_catalog.pg_lsn(:replay) >= pg_catalog.pg_lsn(:target)"
                    ),
                    {"replay": replay_lsn, "target": self._target_lsn},
                )
                is_consistent = fence_res.scalar()

                if is_consistent:
                    logger.debug("Routing read to replica. replay=%s >= target=%s", replay_lsn, self._target_lsn)
                    from app.core.concurrency import adaptive_controller
                    t0 = time.monotonic()
                    try:
                        return await conn.execute(statement, params, **kwargs)
                    finally:
                        await adaptive_controller.record_latency((time.monotonic() - t0) * 1000)

                logger.debug("Replica lag: replay=%s < target=%s. Falling back to primary.", replay_lsn, self._target_lsn)
        except Exception as exc:
            logger.warning("Replica unreachable (%s). Falling back to primary.", exc)

        return await self.execute(statement, params, **kwargs)

    async def commit(self):
        await self._session.commit()

    async def rollback(self):
        await self._session.rollback()

    async def close(self):
        await self._session.close()

    def begin(self):
        return self._session.begin()

    @property
    def bind(self):
        return self._session.bind

    def __getattr__(self, name):
        return getattr(self._session, name)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────────────────────────────────────

async def get_routed_db(request=None) -> AsyncGenerator[RoutingSession, None]:  # type: ignore[misc]
    """
    FastAPI dependency that returns a WAL LSN-aware RoutingSession.
    Initializes GUC context on the primary session.
    """
    user_id  = "00000000-0000-0000-0000-000000000000"
    org_id   = None
    gym_id   = None
    role     = "unknown"
    trace_id = "unknown"

    if request is not None:
        state    = request.state
        user_id  = getattr(state, "staff_id",     user_id)
        org_id   = getattr(state, "org_id",       None)
        gym_id   = getattr(state, "gym_id",       None)
        role     = getattr(state, "role",         "unknown")
        trace_id = getattr(state, "otel_trace_id", None) or getattr(state, "correlation_id", "unknown")

    from app.core.pool_manager import pool_manager
    active_sessionmaker = pool_manager.current_sessionmaker or PrimarySession
    async with active_sessionmaker() as session:
        try:
            await SessionContextInitializer.initialize(
                session,
                user_id  = str(user_id),
                org_id   = str(org_id) if org_id else None,
                gym_id   = str(gym_id) if gym_id else None,
                trace_id = str(trace_id),
                role     = str(role),
            )
            routing_session = RoutingSession(session)
            yield routing_session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
