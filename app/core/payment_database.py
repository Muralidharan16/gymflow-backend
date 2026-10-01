"""Dedicated PAY-24-B payment-provider authority database identity.

The ordinary FastAPI login intentionally cannot inherit ``finance_payment_runtime``.
RI1A binds the isolated login/session only; checkout routes are wired in RI1B after
runtime certification.
"""

from __future__ import annotations

import time
from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import initialize_request_session
from app.core.runtime_principal_attestation import install_connection_identity_guard


def _validated_finance_payment_database_url() -> str | None:
    raw = settings.FINANCE_PAYMENT_DATABASE_URL.strip()
    if not raw:
        return None

    ordinary = make_url(settings.DATABASE_URL)
    payment = make_url(raw)
    if ordinary.database != payment.database:
        raise RuntimeError(
            "FINANCE_PAYMENT_DATABASE_URL must target the same application database as DATABASE_URL"
        )
    if ordinary.username == payment.username:
        raise RuntimeError(
            "FINANCE_PAYMENT_DATABASE_URL must use a distinct PostgreSQL login from DATABASE_URL"
        )

    auth_raw = settings.AUTH_DATABASE_URL.strip()
    if auth_raw and make_url(auth_raw).username == payment.username:
        raise RuntimeError(
            "FINANCE_PAYMENT_DATABASE_URL must use a distinct PostgreSQL login from AUTH_DATABASE_URL"
        )
    return raw


_FINANCE_PAYMENT_DATABASE_URL = _validated_finance_payment_database_url()

finance_payment_async_engine = (
    create_async_engine(
        _FINANCE_PAYMENT_DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=settings.ENVIRONMENT == "development",
    )
    if _FINANCE_PAYMENT_DATABASE_URL
    else None
)

if finance_payment_async_engine is not None and settings.is_production:
    install_connection_identity_guard(
        finance_payment_async_engine.sync_engine,
        "finance_payment",
        _FINANCE_PAYMENT_DATABASE_URL,
    )

FinancePaymentSessionLocal = (
    async_sessionmaker(
        finance_payment_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    if finance_payment_async_engine is not None
    else None
)


async def get_finance_payment_db(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped isolated payment-authority session."""
    if FinancePaymentSessionLocal is None:
        raise RuntimeError(
            "FINANCE_PAYMENT_DATABASE_URL is required for payment-provider authority"
        )

    from app.core.concurrency import adaptive_controller

    async with FinancePaymentSessionLocal() as session:
        started = time.monotonic()
        try:
            await initialize_request_session(
                session,
                request,
                timeout_profile="runtime_default",
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await adaptive_controller.record_latency(
                (time.monotonic() - started) * 1000
            )
