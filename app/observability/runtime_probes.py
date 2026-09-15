"""P8 process-local operational probes.

These probes use only the runtime credentials already owned by the process. They
do not discover cross-tenant business data and never become readiness/business
authority; failures are recorded as telemetry and the supervisor keeps retrying.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError

from app.observability.runtime_metrics import runtime_metrics


logger = logging.getLogger("doers.observability.probes")
_API_POOL_CAPACITY = 30  # database.py: pool_size=10 + max_overflow=20
_API_PROBE_INTERVAL_SECONDS = 15.0


def _api_pool_checked_out(pool) -> int:
    checked_out = getattr(pool, "checkedout", None)
    if callable(checked_out):
        try:
            return max(0, int(checked_out()))
        except Exception:
            return 0
    return 0


async def probe_api_runtime_once() -> None:
    from app.core.database import async_engine
    from app.core.redis import redis_client

    metrics = runtime_metrics()
    pool = async_engine.sync_engine.pool
    metrics.database_pool_snapshot(
        pool="api",
        checked_out=_api_pool_checked_out(pool),
        capacity=_API_POOL_CAPACITY,
    )

    started = time.monotonic()
    connection = None
    try:
        # ``connect`` is the actual QueuePool checkout boundary. Measuring only
        # this await avoids conflating SQL execution latency with pool wait.
        connection = await async_engine.connect()
        metrics.database_pool_wait(
            pool="api",
            duration_ms=(time.monotonic() - started) * 1000,
        )
    except SQLAlchemyTimeoutError:
        metrics.database_pool_timeout(pool="api")
    except DBAPIError:
        metrics.database_disconnect(pool="api")
    except (OSError, ConnectionError):
        metrics.database_disconnect(pool="api")
    except Exception:
        # Unknown probe failures are evidence only; log without destabilizing the API.
        logger.warning("P8 API database probe failed", exc_info=True)
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                metrics.database_disconnect(pool="api")

    try:
        healthy = bool(await redis_client.ping())
    except Exception:
        healthy = False
    metrics.redis_state(role="application", healthy=healthy)


async def api_runtime_probe_loop() -> None:
    while True:
        await probe_api_runtime_once()
        await asyncio.sleep(_API_PROBE_INTERVAL_SECONDS)
