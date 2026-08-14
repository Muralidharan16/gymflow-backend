from __future__ import annotations

import os

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


_ENV = "SYNTHETIC_ORG_TEST_DATABASE_URL"
_EXPECTED_USER = "synthetic_test_runtime"


def _required_url():
    raw = os.environ.get(_ENV)
    if not raw:
        raise RuntimeError(f"{_ENV} is required for synthetic-organization integration tests")

    url = make_url(raw)
    if url.drivername != "postgresql+asyncpg":
        raise RuntimeError(f"{_ENV} must use postgresql+asyncpg")
    if url.username != _EXPECTED_USER:
        raise RuntimeError(
            f"{_ENV} must use the dedicated {_EXPECTED_USER} identity"
        )
    if not url.database or not (
        url.database.endswith("_ci")
        or "_test" in url.database
        or url.database.startswith("test_")
    ):
        raise RuntimeError(f"{_ENV} must target an explicitly disposable test database")

    finance_raw = os.environ.get("FINANCE_CORE_TEST_DATABASE_URL")
    if finance_raw:
        finance = make_url(finance_raw)
        if finance.database != url.database:
            raise RuntimeError(
                f"{_ENV} must target the same disposable database as Finance Core"
            )
        if finance.username == url.username:
            raise RuntimeError(
                "synthetic bootstrap tests must not reuse the Finance Core runtime identity"
            )

    admin_raw = os.environ.get("FINANCE_CORE_TEST_ADMIN_DATABASE_URL")
    if admin_raw and make_url(admin_raw).username == url.username:
        raise RuntimeError(
            "synthetic bootstrap tests must not reuse the Finance Core admin identity"
        )

    return url


_synthetic_url = _required_url()
# Pytest-asyncio may execute independent tests on distinct event loops. asyncpg
# connections are loop-affine, so a module-level QueuePool can illegally hand a
# connection created on a closed/foreign loop to the next test. This dedicated
# non-production bootstrap lane is intentionally low-volume: NullPool gives each
# session a fresh connection bound to the currently running loop and preserves
# the separate synthetic_test_runtime database identity without altering Finance
# Core production/runtime pooling behavior.
synthetic_engine = create_async_engine(
    _synthetic_url,
    pool_pre_ping=True,
    poolclass=NullPool,
)
SyntheticOrgSessionLocal = async_sessionmaker(
    bind=synthetic_engine,
    expire_on_commit=False,
)
