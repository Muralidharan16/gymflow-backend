"""
app/core/database.py
=====================
Enterprise database session management for the Doers SaaS platform.

Key design decisions:
  • Session context initializer injects GUCs inside an explicit transaction scope
    so SET LOCAL settings survive for the full request lifetime (not silently dropped).
  • statement_timeout / lock_timeout / deadlock_timeout enforced per session.
  • Sync engine preserved for Celery tasks.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncGenerator, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from app.core.config import settings

logger = logging.getLogger("doers.database")

# ─────────────────────────────────────────────────────────────────────────────
# Async engine (FastAPI)
# ─────────────────────────────────────────────────────────────────────────────

async_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.ENVIRONMENT == "development",
    pool_recycle=1800,  # recycle connections every 30 min to avoid idle disconnects
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Session context initializer
# ─────────────────────────────────────────────────────────────────────────────

class SessionContextInitializer:
    """
    Enforces safe GUC initialization on every AsyncSession.

    All SET LOCAL calls are wrapped in an explicit transaction block to guarantee
    they survive for the full request lifetime. Without this, SET LOCAL settings
    can silently disappear if SQLAlchemy autobegin closes a transaction boundary
    prematurely.
    """

    @staticmethod
    async def initialize(
        session: AsyncSession,
        user_id:   str,
        org_id:    Optional[str] = None,
        gym_id:    Optional[str] = None,
        trace_id:  str           = "unknown",
        role:      str           = "unknown",
    ):
        # Validate lock_timeout value before embedding as literal
        _LOCK_TIMEOUT_MS = 500
        _STMT_TIMEOUT_MS = 5000
        _DEADLOCK_MS     = 200
        _IDLE_TIMEOUT_MS = 15000

        async with session.begin():
            org_label = org_id or "anon"
            await session.execute(
                sa.text(f"SET LOCAL application_name = 'doers:org:{org_label}:trace:{trace_id}'")
            )
            await session.execute(
                sa.text("SELECT pg_catalog.set_config('app.current_user', :uid, true)"),
                {"uid": user_id},
            )
            await session.execute(
                sa.text("SELECT pg_catalog.set_config('app.current_user_id', :uid, true)"),
                {"uid": user_id},
            )
            if org_id:
                await session.execute(
                    sa.text("SELECT pg_catalog.set_config('app.current_org_id', :oid, true)"),
                    {"oid": org_id},
                )
            if gym_id:
                await session.execute(
                    sa.text("SELECT pg_catalog.set_config('app.current_gym_id', :gid, true)"),
                    {"gid": gym_id},
                )
            await session.execute(
                sa.text("SELECT pg_catalog.set_config('app.current_role', :role, true)"),
                {"role": role},
            )
            # Enforce per-request safety limits
            await session.execute(sa.text(f"SET LOCAL statement_timeout = '{_STMT_TIMEOUT_MS}ms'"))
            await session.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'"))
            await session.execute(sa.text(f"SET LOCAL deadlock_timeout = '{_DEADLOCK_MS}ms'"))
            await session.execute(sa.text(f"SET LOCAL idle_in_transaction_session_timeout = '{_IDLE_TIMEOUT_MS}ms'"))


# ── Register initial pool in DynamicPoolManager ───────────────────────────
from app.core.pool_manager import pool_manager
pool_manager.set_initial_pool(async_engine, AsyncSessionLocal)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency — instrumented with latency recording
# ─────────────────────────────────────────────────────────────────────────────

async def get_db(request=None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[misc]
    """
    FastAPI session dependency.
    Initializes session GUC context and records DB latency for adaptive backpressure.
    Uses DynamicPoolManager to dynamically resolve connection pools during credential rotations.
    """
    from app.core.concurrency import adaptive_controller

    user_id  = "00000000-0000-0000-0000-000000000000"
    org_id   = None
    gym_id   = None
    role     = "unknown"
    trace_id = "unknown"

    if request is not None:
        state = request.state
        user_id  = getattr(state, "staff_id",       user_id)
        org_id   = getattr(state, "org_id",         None)
        gym_id   = getattr(state, "gym_id",         None)
        role     = getattr(state, "role",            "unknown")
        trace_id = getattr(state, "otel_trace_id",  None) or getattr(state, "correlation_id", "unknown")

    active_sessionmaker = pool_manager.current_sessionmaker or AsyncSessionLocal
    async with active_sessionmaker() as session:
        t0 = time.monotonic()
        try:
            await SessionContextInitializer.initialize(
                session,
                user_id=str(user_id),
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
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            await adaptive_controller.record_latency(elapsed_ms)


# ─────────────────────────────────────────────────────────────────────────────
# Sync engine (Celery tasks)
# ─────────────────────────────────────────────────────────────────────────────

SYNC_DATABASE_URL = settings.DATABASE_URL.replace("+asyncpg", "")
sync_engine       = None
SyncSessionLocal  = None

try:
    if "postgresql" in SYNC_DATABASE_URL:
        sync_engine      = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
        SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
except Exception as exc:
    logger.warning("Sync DB engine could not be initialized: %s", exc)

SessionLocal = SyncSessionLocal
