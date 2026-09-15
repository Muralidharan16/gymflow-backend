"""P8 operational snapshot for broker queues and Redis delivery dependencies."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import redis.asyncio as redis_async
from celery import shared_task

from app.core.config import settings
from app.observability.runtime_metrics import runtime_metrics


logger = logging.getLogger("doers.observability.runtime_snapshot")
_QUEUES = ("worker", "lifecycle-maintenance")
_PUBLISHED_AT_HEADER = "doers_published_at_unix"


def _published_at(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        value = payload.get("headers", {}).get(_PUBLISHED_AT_HEADER)
        timestamp = float(value)
        return timestamp if timestamp > 0 else None
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None


async def _queue_snapshot(client, queue: str, now: float) -> tuple[int, float]:
    depth = int(await client.llen(queue) or 0)
    if depth <= 0:
        return 0, 0.0

    # Kombu's list direction is an implementation detail. Inspect both ends and
    # choose the oldest bounded publish timestamp rather than assuming LPUSH/RPOP.
    first, last = await asyncio.gather(
        client.lindex(queue, 0),
        client.lindex(queue, -1),
    )
    timestamps = [value for value in (_published_at(first), _published_at(last)) if value]
    if not timestamps:
        return depth, 0.0
    return depth, max(0.0, now - min(timestamps))


async def _run_runtime_operational_snapshot() -> dict[str, Any]:
    metrics = runtime_metrics()
    now = time.time()
    result: dict[str, Any] = {"queues": {}, "redis": {}}

    broker = redis_async.Redis.from_url(
        settings.CELERY_BROKER_URL,
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    backend = redis_async.Redis.from_url(
        settings.CELERY_RESULT_BACKEND,
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        try:
            broker_ok = bool(await broker.ping())
        except Exception:
            broker_ok = False
        metrics.redis_state(role="broker", healthy=broker_ok)
        result["redis"]["broker"] = broker_ok

        if broker_ok:
            for queue in _QUEUES:
                try:
                    depth, oldest_age = await _queue_snapshot(broker, queue, now)
                except Exception:
                    logger.warning(
                        "P8 broker queue snapshot failed",
                        extra={"queue": queue},
                        exc_info=True,
                    )
                    continue
                metrics.queue_snapshot(
                    queue=queue,
                    depth=depth,
                    oldest_age_seconds=oldest_age,
                    # Celery broker queues do not own durable DLQ truth. Durable
                    # dead-letter counts are sourced from PostgreSQL snapshots.
                    dead_letters=0,
                )
                result["queues"][queue] = {
                    "depth": depth,
                    "oldest_age_seconds": oldest_age,
                }

        try:
            backend_ok = bool(await backend.ping())
        except Exception:
            backend_ok = False
        metrics.redis_state(role="result_backend", healthy=backend_ok)
        result["redis"]["result_backend"] = backend_ok
        return result
    finally:
        await broker.aclose()
        await backend.aclose()


@shared_task(
    name="app.tasks.runtime_observability.snapshot",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def run_runtime_operational_snapshot() -> dict[str, Any]:
    return asyncio.run(_run_runtime_operational_snapshot())
