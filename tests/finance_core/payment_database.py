from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


_PAYMENT_ENV = "FINANCE_PAYMENT_DATABASE_URL"
_FINANCE_ENV = "FINANCE_CORE_TEST_DATABASE_URL"
_ADMIN_ENV = "FINANCE_CORE_TEST_ADMIN_DATABASE_URL"

_EXPECTED_PAYMENT_USER = "finance_payment_deployment"


def _required_payment_url() -> str:
    raw = os.environ.get(_PAYMENT_ENV)

    if not raw:
        raise RuntimeError(
            f"{_PAYMENT_ENV} is required for Finance provider-effect tests"
        )

    payment = make_url(raw)

    if payment.drivername != "postgresql+asyncpg":
        raise RuntimeError(
            f"{_PAYMENT_ENV} must use postgresql+asyncpg"
        )

    if payment.username != _EXPECTED_PAYMENT_USER:
        raise RuntimeError(
            f"{_PAYMENT_ENV} must use {_EXPECTED_PAYMENT_USER}"
        )

    database = str(payment.database or "")

    if not database or not (
        database.endswith("_ci")
        or "_test" in database
        or database.startswith("test_")
    ):
        raise RuntimeError(
            f"{_PAYMENT_ENV} must target an explicitly disposable database"
        )

    finance_raw = os.environ.get(_FINANCE_ENV)

    if not finance_raw:
        raise RuntimeError(
            f"{_FINANCE_ENV} is required alongside {_PAYMENT_ENV}"
        )

    finance = make_url(finance_raw)

    if finance.database != payment.database:
        raise RuntimeError(
            "Finance payment tests must use the same disposable database "
            "as Finance Core"
        )

    if finance.username == payment.username:
        raise RuntimeError(
            "Finance payment identity must remain distinct from "
            "the ordinary Finance test identity"
        )

    admin_raw = os.environ.get(_ADMIN_ENV)

    if admin_raw:
        admin = make_url(admin_raw)

        if admin.username == payment.username:
            raise RuntimeError(
                "Finance payment identity must remain distinct from "
                "the migration/admin identity"
            )

    return raw


@asynccontextmanager
async def finance_payment_session(
    *,
    organization_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """Open one isolated finance_payment_runtime test transaction.

    This mirrors the production PAY-24 provider-effect boundary:
    local checkout preparation/reserve remains on the ordinary app/Finance
    identity while claim/finish executes through the dedicated payment login.
    """

    raw = _required_payment_url()

    engine = create_async_engine(
        raw,
        poolclass=NullPool,
        pool_pre_ping=True,
        echo=False,
    )

    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    try:
        async with factory() as session:
            identity = (
                await session.execute(
                    text(
                        """
                        SELECT
                            session_user::text,
                            current_user::text,
                            pg_catalog.pg_has_role(
                                session_user,
                                'finance_payment_runtime',
                                'MEMBER'
                            ),
                            pg_catalog.pg_has_role(
                                session_user,
                                'app_runtime',
                                'MEMBER'
                            )
                        """
                    )
                )
            ).one()

            if identity[0] != _EXPECTED_PAYMENT_USER:
                raise RuntimeError(
                    "Finance payment test session_user drift"
                )

            if identity[1] != _EXPECTED_PAYMENT_USER:
                raise RuntimeError(
                    "Finance payment test current_user drift"
                )

            if identity[2] is not True:
                raise RuntimeError(
                    "Finance payment test login lacks "
                    "finance_payment_runtime"
                )

            if identity[3] is True:
                raise RuntimeError(
                    "Finance payment test login unexpectedly reaches "
                    "app_runtime"
                )

            await session.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'app.current_org_id',"
                    ":organization_id,"
                    "true)"
                ),
                {
                    "organization_id": str(
                        organization_id
                    )
                },
            )

            yield session
    finally:
        await engine.dispose()
