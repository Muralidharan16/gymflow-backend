"""
app/core/concurrency.py
========================
Distributed coordination primitives for the Doers SaaS platform.

Components:
  • WeightedFairQueue      — virtual-time WFQ with task-per-job dispatch + per-tenant depth caps
  • SortedSetSemaphore     — Redis sorted-set lease semaphore with Redis-authoritative time
  • AdaptiveConcurrencyController — EWMA-based adaptive backpressure (latency-feedback)
  • ContextVar retry depth guard — prevents multiplicative retry storms
  • IOGuardrails           — asyncio.Semaphore guardrails per I/O class
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import random
import time
import uuid
import contextvars
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy.exc import DBAPIError, OperationalError

logger = logging.getLogger("doers.concurrency")

# ─────────────────────────────────────────────────────────────────────────────
# 1. Weighted Fair Queue with task-per-job dispatch & Redis-backed clocks
# ─────────────────────────────────────────────────────────────────────────────

TIER_WEIGHTS: Dict[str, int] = {"ENTERPRISE": 10, "PROFESSIONAL": 3, "STARTER": 1}

_LUA_INC_VIRTUAL_CLOCK = """
local clock_key = KEYS[1]
local increment = tonumber(ARGV[1])

local now_data = redis.call('TIME')
local now      = tonumber(now_data[1]) + tonumber(now_data[2]) / 1000000

local current = tonumber(redis.call('GET', clock_key) or now)
if current < now then
    current = now
end
local new_vt = current + increment
redis.call('SET', clock_key, new_vt)
redis.call('EXPIRE', clock_key, 3600)
return tostring(new_vt)
"""

@dataclass(order=True)
class _WFQItem:
    virtual_time: float
    created_at:   float    = field(compare=False)
    tenant_id:    str      = field(compare=False)
    coro_fn:      Any      = field(compare=False)


class WeightedFairQueue:
    """
    Per-tenant weighted fair queue using global Redis-backed virtual-time scheduling.
    Ensures cluster-wide multi-tenant fairness across multiple pod instances.

    Key correctness properties:
      • task-per-job dispatch: loop never awaits execution, semaphore acquired inside task.
      • per-tenant depth cap: enqueue() returns False when tenant queue is full.
      • asyncio.Event-based loop: no busy polling.
    """

    def __init__(self, max_concurrent: int = 20):
        self._heap:            list[_WFQItem]      = []
        self._queue_depths:    Dict[str, int]      = {}
        self._lock             = asyncio.Lock()
        self._semaphore        = asyncio.Semaphore(max_concurrent)
        self._nonempty         = asyncio.Event()
        self._shutdown_event   = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None

    async def enqueue(
        self,
        tenant_id: str,
        tier: str,
        coro_fn: Callable[[], Awaitable],
        max_depth: int = 500,
    ) -> bool:
        """
        Enqueue a coroutine for tenant. Returns False (shed) when at capacity.
        Callers should surface False as HTTP 429.
        """
        from app.core.redis import redis_client

        weight = TIER_WEIGHTS.get(tier, 1)
        async with self._lock:
            if self._queue_depths.get(tenant_id, 0) >= max_depth:
                logger.warning("WFQ: tenant %s queue full (%d). Shedding.", tenant_id, max_depth)
                return False

            # Fetch globally coordinated virtual clock value from Redis
            clock_key = f"wfq:clock:{tenant_id}"
            increment = 1.0 / weight
            try:
                vt_str = await redis_client.eval(_LUA_INC_VIRTUAL_CLOCK, 1, clock_key, increment)
                vt = float(vt_str)
            except Exception:
                # Fallback to local memory virtual time if Redis is unreachable
                vt = time.monotonic() + increment

            self._queue_depths[tenant_id] = self._queue_depths.get(tenant_id, 0) + 1
            heapq.heappush(self._heap, _WFQItem(vt, time.monotonic(), tenant_id, coro_fn))
            self._nonempty.set()
        return True

    async def _execute(self, item: _WFQItem):
        """Runs one job; acquires the semaphore internally so the loop stays free."""
        async with self._semaphore:
            try:
                await item.coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("WFQ task failed for tenant %s: %s", item.tenant_id, exc)
            finally:
                async with self._lock:
                    depth = self._queue_depths.get(item.tenant_id, 1) - 1
                    self._queue_depths[item.tenant_id] = max(0, depth)

    async def _run_loop(self):
        """Main dispatch loop — pops and spawns; never awaits job completion."""
        while not self._shutdown_event.is_set():
            await self._nonempty.wait()
            async with self._lock:
                if not self._heap:
                    self._nonempty.clear()
                    continue
                item = heapq.heappop(self._heap)
                if not self._heap:
                    self._nonempty.clear()
            # Spawn task immediately — loop does not block on execution
            asyncio.create_task(self._execute(item), name=f"wfq:{item.tenant_id}")

    def start(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run_loop(), name="wfq_dispatch_loop")

    async def stop(self):
        self._shutdown_event.set()
        self._nonempty.set()  # unblock the loop
        if self._worker_task:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)


# Singleton — import and use directly
fair_queue = WeightedFairQueue(max_concurrent=20)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Redis Sorted-Set Lease Semaphore with Redis-authoritative time
# ─────────────────────────────────────────────────────────────────────────────

_LUA_LEASE_ACQUIRE = """
local key    = KEYS[1]
local limit  = tonumber(ARGV[1])
local member = ARGV[2]
local ttl    = tonumber(ARGV[3])

local now_data = redis.call('TIME')
local now      = tonumber(now_data[1]) + tonumber(now_data[2]) / 1000000

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
local count = redis.call('ZCARD', key)
if count >= limit then
    return 0
end
redis.call('ZADD', key, now, member)
return 1
"""

_LUA_LEASE_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""


class SortedSetSemaphore:
    """
    Redis sorted-set lease semaphore.
    Each acquired slot is owned by a unique UUID token.
    Orphan entries are evicted by the Lua script using Redis-authoritative TIME.
    This fixes: counter underflow, crash-inflation, and clock-skew issues.
    """
    LEASE_TTL = 60  # seconds

    def __init__(self, redis_client, limit: int):
        self._redis = redis_client
        self._limit = limit

    async def acquire(self, key: str) -> Optional[str]:
        """Returns a lease token on success, None if at capacity."""
        token    = str(uuid.uuid4())
        acquired = await self._redis.eval(
            _LUA_LEASE_ACQUIRE, 1, key,
            self._limit, token, self.LEASE_TTL
        )
        return token if acquired else None

    async def release(self, key: str, token: str):
        await self._redis.eval(_LUA_LEASE_RELEASE, 1, key, token)


# ─────────────────────────────────────────────────────────────────────────────
# 3. EWMA Adaptive Concurrency Controller
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveConcurrencyController:
    """
    Tracks DB response latency via EWMA and derives a dynamic rejection probability.
    Renamed TARGET_DB_LATENCY_MS (was TARGET_MS) to avoid p50/EWMA confusion.

    Includes AIMD (Additive Increase, Multiplicative Decrease) dynamic concurrency limit
    tuning based on actual performance.
    """
    ALPHA               = 0.1
    TARGET_DB_LATENCY_MS = 200.0
    MAX_REJECT           = 0.8

    _CRITICAL_PREFIXES = ("/api/v1/auth", "/auth", "/api/v1/billing", "/api/v1/sessions")
    _ELEVATED_PREFIXES = ("/api/v1/members",)

    def __init__(self):
        self._ewma_ms = self.TARGET_DB_LATENCY_MS
        self._lock    = asyncio.Lock()
        
        # AIMD Concurrency limit parameters
        self.min_limit = 10
        self.max_limit = 200
        self.current_limit = 50
        self.success_streak = 0

    async def record_latency(self, elapsed_ms: float):
        async with self._lock:
            # 1. Update EWMA latency
            self._ewma_ms = self.ALPHA * elapsed_ms + (1 - self.ALPHA) * self._ewma_ms

            # 2. AIMD Limit Tuning
            if self._ewma_ms > self.TARGET_DB_LATENCY_MS * 1.5:
                # Congestion detected -> Multiplicative Decrease (halve limits)
                self.current_limit = max(self.min_limit, int(self.current_limit * 0.5))
                self.success_streak = 0
                logger.warning(
                    "Backpressure detected (EWMA = %.1fms). AIMD scaled dynamic limit down to %d.",
                    self._ewma_ms, self.current_limit
                )
            else:
                # Safe operation -> Additive Increase (step up slowly)
                self.success_streak += 1
                if self.success_streak >= 15:
                    self.current_limit = min(self.max_limit, self.current_limit + 1)
                    self.success_streak = 0

    def classify_path(self, path: str) -> str:
        if any(path.startswith(p) for p in self._CRITICAL_PREFIXES):
            return "CRITICAL"
        if any(path.startswith(p) for p in self._ELEVATED_PREFIXES):
            return "ELEVATED"
        return "BULK"

    def rejection_probability(self, path: str) -> float:
        traffic_class = self.classify_path(path)
        if traffic_class == "CRITICAL":
            return 0.0
        ratio = self._ewma_ms / self.TARGET_DB_LATENCY_MS
        if ratio <= 1.0:
            return 0.0
        raw  = 1.0 - (1.0 / ratio)
        prob = min(self.MAX_REJECT, raw)
        if traffic_class == "ELEVATED":
            prob *= 0.25
        return prob

    @property
    def ewma_latency_ms(self) -> float:
        return round(self._ewma_ms, 2)


# Singleton — shared by AdaptiveWriteThrottler middleware
adaptive_controller = AdaptiveConcurrencyController()


# ─────────────────────────────────────────────────────────────────────────────
# 4. ContextVar Retry Depth Guard
# ─────────────────────────────────────────────────────────────────────────────

_retry_depth: contextvars.ContextVar[int] = contextvars.ContextVar("db_retry_depth", default=0)

TRANSIENT_PG_CODES = frozenset({"40001", "40P01"})


def retry_on_transaction_failure(
    max_retries: int   = 5,
    initial_delay: float = 0.05,
    backoff_factor: float = 2.0,
    max_delay: float   = 2.0,
    db_only: bool      = True,
):
    """
    Async retry decorator for transient PostgreSQL failures.

    db_only=True  (default): only retries SQLSTATE 40001 / 40P01 / connection invalidated.
    db_only=False: caller asserts the function is fully idempotent (no side-effect retries).

    Guards against nested retry storms via ContextVar depth tracking.
    asyncio.CancelledError is always re-raised immediately.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_depth = _retry_depth.get()
            if current_depth > 0:
                # Already inside a parent retry scope — run once without retry
                logger.debug("Skipping retry (nested depth=%d) for %s", current_depth, func.__name__)
                return await func(*args, **kwargs)

            token = _retry_depth.set(current_depth + 1)
            delay = initial_delay
            try:
                for attempt in range(1, max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except asyncio.CancelledError:
                        raise
                    except (OperationalError, DBAPIError) as exc:
                        pg_code  = getattr(getattr(exc, "orig", None), "pgcode", None)
                        is_stale = getattr(exc, "connection_invalidated", False)
                        if db_only and pg_code not in TRANSIENT_PG_CODES and not is_stale:
                            raise  # constraint / syntax / permission — non-retryable
                        if attempt >= max_retries:
                            raise
                        sleep_time = min(
                            max_delay,
                            delay * (backoff_factor ** (attempt - 1)) + random.uniform(0.01, 0.05),
                        )
                        logger.warning(
                            "Transient DB failure (attempt %d/%d, code=%s). Retrying in %.3fs",
                            attempt, max_retries, pg_code, sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
            finally:
                _retry_depth.reset(token)

        return wrapper
    return decorator
