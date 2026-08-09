"""
app/core/database.py
=====================
Enterprise database session management for the Doers SaaS platform.

Key design decisions:
  • Verified request identity/tenant context is stored on the SQLAlchemy Session,
    not on a pooled connection.
  • Every transaction automatically receives the session context with PostgreSQL
    ``set_config(..., is_local=true)`` before application SQL executes. This keeps
    RLS/audit GUCs correct even when a route commits and starts another transaction,
    while preventing tenant context from leaking through the connection pool.
  • User-settable statement, lock, and idle-in-transaction timeouts are enforced
    on every transaction.
  • Cluster-level/privileged lock-detection settings such as ``deadlock_timeout``
    remain an administrator-owned PostgreSQL configuration boundary.
  • Sync engine is preserved for Celery tasks and uses the declared Psycopg 3 driver.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncGenerator, Optional

import sqlalchemy as sa
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("doers.database")

_SESSION_CONTEXT_KEY = "doers.request_db_context"
_ALLOWED_PRINCIPAL_TYPES = frozenset(
    {"owner", "organization_user", "legacy_gym_owner"}
)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

_LOCK_TIMEOUT_MS = 500
_STMT_TIMEOUT_MS = 5000
_IDLE_TIMEOUT_MS = 15000


# ─────────────────────────────────────────────────────────────────────────────
# Async engine (FastAPI)
# ─────────────────────────────────────────────────────────────────────────────

async_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.ENVIRONMENT == "development",
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async_session_maker = AsyncSessionLocal


# ─────────────────────────────────────────────────────────────────────────────
# Request/session database context
# ─────────────────────────────────────────────────────────────────────────────

def _validate_principal_type(principal_type: Optional[str]) -> Optional[str]:
    if principal_type is None:
        return None
    normalized = str(principal_type).strip().lower()
    if normalized not in _ALLOWED_PRINCIPAL_TYPES:
        raise ValueError(f"Unsupported database principal type: {principal_type!r}")
    return normalized


def _context_settings(context: dict[str, Optional[str]]):
    """Yield PostgreSQL settings that must be transaction-local.

    Values are passed through ``pg_catalog.set_config`` parameters rather than SQL
    interpolation. Missing optional values are intentionally not written: because
    every setting is LOCAL, PostgreSQL clears the previous transaction's value
    before the next transaction/pooled borrower can observe it.
    """

    org_id = context.get("org_id")
    trace_id = context.get("trace_id") or "unknown"
    org_label = org_id or "anon"

    yield "application_name", f"doers:org:{org_label}:trace:{trace_id}"

    mapping = (
        ("app.current_user", context.get("principal_id")),
        ("app.current_user_id", context.get("principal_id")),
        ("app.current_principal_type", context.get("principal_type")),
        ("app.current_org_id", org_id),
        ("app.current_gym_id", context.get("gym_id")),
        ("app.current_role", context.get("role") or "unknown"),
        ("app.request_id", context.get("request_id")),
        ("app.ip_address", context.get("ip_address")),
        ("app.user_agent", context.get("user_agent")),
        ("app.internal_maintenance", context.get("internal_maintenance")),
    )
    for name, value in mapping:
        if value is not None:
            yield name, str(value)

    yield "statement_timeout", f"{_STMT_TIMEOUT_MS}ms"
    yield "lock_timeout", f"{_LOCK_TIMEOUT_MS}ms"
    yield "idle_in_transaction_session_timeout", f"{_IDLE_TIMEOUT_MS}ms"


def _apply_context_sync(connection, context: dict[str, Optional[str]]) -> None:
    statement = sa.text(
        "SELECT pg_catalog.set_config(:setting_name, :setting_value, true)"
    )
    for name, value in _context_settings(context):
        connection.execute(
            statement,
            {"setting_name": name, "setting_value": value},
        )


@event.listens_for(Session, "after_begin")
def _apply_request_context_after_begin(session, transaction, connection) -> None:
    """Install verified context on *every* transaction for this Session.

    This is intentionally a Session event rather than a pool/connection event:
    context follows one logical request/session and cannot bleed into the next
    borrower of a pooled PostgreSQL connection. Re-applying on transaction begin
    also makes route/service commits safe: the next transaction receives the same
    verified RLS/audit context automatically.
    """

    context = session.info.get(_SESSION_CONTEXT_KEY)
    if context:
        _apply_context_sync(connection, context)


async def update_session_context(
    session: AsyncSession,
    *,
    principal_id: Optional[str] = None,
    principal_type: Optional[str] = None,
    org_id: Optional[str] = None,
    gym_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    role: Optional[str] = None,
    request_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    internal_maintenance: Optional[str] = None,
) -> None:
    """Update Session-owned context and apply it to the active transaction.

    Routers/services that learn additional verified context after dependency
    creation (for example request metadata or an onboarding owner) call this
    helper. Future transactions inherit the updated values automatically.
    """

    context = dict(session.info.get(_SESSION_CONTEXT_KEY, {}))
    updates = {
        "principal_id": principal_id,
        "principal_type": _validate_principal_type(principal_type)
        if principal_type is not None
        else None,
        "org_id": org_id,
        "gym_id": gym_id,
        "trace_id": trace_id,
        "role": role,
        "request_id": request_id,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "internal_maintenance": internal_maintenance,
    }
    for key, value in updates.items():
        if value is not None:
            context[key] = str(value)

    session.info[_SESSION_CONTEXT_KEY] = context

    if session.in_transaction():
        statement = sa.text(
            "SELECT pg_catalog.set_config(:setting_name, :setting_value, true)"
        )
        for name, value in _context_settings(context):
            await session.execute(
                statement,
                {"setting_name": name, "setting_value": value},
            )


class SessionContextInitializer:
    """Attach verified request context to an ``AsyncSession``.

    No transaction is opened here. The ``after_begin`` hook above installs the
    context when SQLAlchemy begins the actual request transaction, and repeats it
    for every later transaction on the same Session.
    """

    @staticmethod
    async def initialize(
        session: AsyncSession,
        user_id: str,
        org_id: Optional[str] = None,
        gym_id: Optional[str] = None,
        trace_id: str = "unknown",
        role: str = "unknown",
        principal_type: Optional[str] = None,
    ) -> None:
        normalized_principal_type = _validate_principal_type(principal_type)
        if user_id != _ZERO_UUID and normalized_principal_type is None:
            # Existing pre-hardening access tokens are owner tokens. Keeping this
            # compatibility default avoids invalidating a 15-minute token window;
            # all newly issued tokens carry an explicit principal_type claim.
            normalized_principal_type = "owner"

        session.info[_SESSION_CONTEXT_KEY] = {
            "principal_id": str(user_id),
            "principal_type": normalized_principal_type,
            "org_id": str(org_id) if org_id else None,
            "gym_id": str(gym_id) if gym_id else None,
            "trace_id": str(trace_id),
            "role": str(role),
        }


# ── Register initial pool in DynamicPoolManager ───────────────────────────
from app.core.pool_manager import pool_manager
pool_manager.set_initial_pool(async_engine, AsyncSessionLocal)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency — instrumented with latency recording
# ─────────────────────────────────────────────────────────────────────────────

async def get_db(request=None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[misc]
    """Yield a request Session with transaction-safe RLS/audit context."""
    from app.core.concurrency import adaptive_controller

    principal_id = _ZERO_UUID
    principal_type = None
    org_id = None
    gym_id = None
    role = "unknown"
    trace_id = "unknown"

    if request is not None:
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
        trace_id = getattr(state, "otel_trace_id", None) or getattr(
            state, "correlation_id", "unknown"
        )

    active_sessionmaker = pool_manager.current_sessionmaker or AsyncSessionLocal
    async with active_sessionmaker() as session:
        t0 = time.monotonic()
        try:
            await SessionContextInitializer.initialize(
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
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            await adaptive_controller.record_latency(elapsed_ms)


# ─────────────────────────────────────────────────────────────────────────────
# Sync engine (Celery tasks)
# ─────────────────────────────────────────────────────────────────────────────

SYNC_DATABASE_URL = settings.DATABASE_URL.replace("+asyncpg", "+psycopg")
sync_engine = None
SyncSessionLocal = None

try:
    if "postgresql" in SYNC_DATABASE_URL:
        sync_engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
        SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
except Exception as exc:
    logger.warning("Sync DB engine could not be initialized: %s", exc)

SessionLocal = SyncSessionLocal
