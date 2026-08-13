"""
app/core/supervisor.py
=======================
Structured background task supervision tree for the Doers SaaS platform.

The FastAPI process owns only API-bound background coroutines. PostgreSQL
partition DDL is infrastructure-owned and is intentionally absent from this
supervisor. P2D attests the API/auth database bindings before any supervised
database work starts.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("doers.supervisor")

try:
    import contextvars
    from opentelemetry import context as otel_context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    _PROPAGATOR = TraceContextTextMapPropagator()
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


def create_traced_task(coro, *, name: Optional[str] = None) -> asyncio.Task:
    if not _OTEL_AVAILABLE:
        return asyncio.create_task(coro, name=name)
    carrier = {}
    _PROPAGATOR.inject(carrier)
    ctx_copy = contextvars.copy_context()

    async def traced_wrapper():
        extracted = _PROPAGATOR.extract(carrier)
        token = otel_context.attach(extracted)
        try:
            return await coro
        finally:
            otel_context.detach(token)
    return asyncio.create_task(ctx_copy.run(traced_wrapper), name=name)


class BackgroundWorkerSupervisor:
    _MAX_RESTARTS = 10
    _BASE_RESTART_DELAY = 5.0

    def __init__(self):
        self.tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

    async def start_worker(self, name: str, coro_fn: Callable[..., Awaitable], *args, **kwargs):
        async def wrapper():
            restarts = 0
            restart_gap = self._BASE_RESTART_DELAY
            while not self._shutdown_event.is_set():
                try:
                    await coro_fn(*args, **kwargs)
                    restarts = 0
                    restart_gap = self._BASE_RESTART_DELAY
                except asyncio.CancelledError:
                    logger.info("Worker '%s' cancelled cleanly.", name)
                    return
                except Exception as exc:
                    restarts += 1
                    if restarts > self._MAX_RESTARTS:
                        logger.critical("Worker '%s' exceeded restart budget (%d). Giving up permanently.", name, self._MAX_RESTARTS, exc_info=True)
                        return
                    restart_gap = min(120.0, restart_gap * 2)
                    logger.error("Worker '%s' crashed (attempt %d/%d). Restarting in %.1fs: %s", name, restarts, self._MAX_RESTARTS, restart_gap, exc)
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=restart_gap)
                        return
                    except asyncio.TimeoutError:
                        pass
        task = asyncio.create_task(wrapper(), name=name)
        self.tasks.append(task)
        logger.info("Supervisor started worker: %s", name)

    async def graceful_shutdown(self, drain_timeout: float = 30.0):
        logger.info("Initiating graceful supervisor shutdown...")
        self._shutdown_event.set()
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        for name, result in zip([t.get_name() for t in self.tasks], results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("Worker '%s' exited with error: %s", name, result)
        logger.info("All supervised workers drained. Supervisor shutdown complete.")


supervisor = BackgroundWorkerSupervisor()


async def _zombie_reclaim_loop():
    from app.core.database import AsyncSessionLocal
    from app.core.idempotency import IdempotencyEngine
    while True:
        try:
            async with asyncio.timeout(10):
                async with AsyncSessionLocal() as db:
                    await IdempotencyEngine.reclaim_zombies(db, stale_threshold_sec=30)
        except asyncio.TimeoutError:
            logger.warning("Zombie reclaim loop timed out.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Zombie reclaim error: %s", exc)
        await asyncio.sleep(60)


async def _anchor_key_archive_loop():
    from app.core.database import AsyncSessionLocal
    from app.core.idempotency import IdempotencyEngine
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            async with AsyncSessionLocal() as db:
                await IdempotencyEngine.archive_expired_anchor_keys(db, retention_hours=48)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Anchor key archive error: %s", exc)


async def _lock_registry_sweep_loop():
    from app.core.crypto import EnvelopeEncryptionProvider
    while True:
        await asyncio.sleep(300)
        try:
            async with asyncio.timeout(5):
                evicted = await EnvelopeEncryptionProvider._lock_registry.sweep_stale()
                if evicted:
                    logger.debug("Lock registry sweep: evicted %d stale locks.", evicted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Lock registry sweep error: %s", exc)


async def _kms_bulkhead_sweep_loop():
    from app.core.crypto import kms_bulkhead
    while True:
        await asyncio.sleep(600)
        try:
            await kms_bulkhead.sweep_idle_breakers()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("KMS bulkhead sweep error: %s", exc)


async def _dek_registry_startup():
    from app.core.crypto import EnvelopeEncryptionProvider
    from app.core.database import AsyncSessionLocal

    async def _db_dek_lookup(tenant_id: str, key_version: int) -> bytes:
        async with AsyncSessionLocal() as db:
            import sqlalchemy as sa
            res = await db.execute(sa.text("""
                SELECT encrypted_dek FROM public.encryption_key_registry
                WHERE tenant_id = :tid AND key_version = :ver
            """), {"tid": tenant_id, "ver": key_version})
            row = res.fetchone()
            if not row:
                raise ValueError(f"DEK not found: tenant={tenant_id} version={key_version}")
            return bytes(row.encrypted_dek)
    EnvelopeEncryptionProvider.register_dek_lookup(_db_dek_lookup)
    logger.info("DEK registry lookup registered into EnvelopeEncryptionProvider.")


async def _wfq_start_loop():
    from app.core.concurrency import fair_queue
    fair_queue.start()
    await asyncio.Event().wait()


async def _attest_fastapi_database_bindings() -> None:
    from app.core.config import settings
    if not settings.is_production:
        return
    from app.core.runtime_principal_attestation import attest_configured_runtime_bindings
    await asyncio.to_thread(attest_configured_runtime_bindings, ("api", "auth"))


@asynccontextmanager
async def platform_lifespan():
    await _attest_fastapi_database_bindings()
    await _dek_registry_startup()
    await supervisor.start_worker("zombie_reclaim", _zombie_reclaim_loop)
    await supervisor.start_worker("anchor_key_archive", _anchor_key_archive_loop)
    await supervisor.start_worker("lock_registry_sweep", _lock_registry_sweep_loop)
    await supervisor.start_worker("kms_bulkhead_sweep", _kms_bulkhead_sweep_loop)
    await supervisor.start_worker("wfq_dispatcher", _wfq_start_loop)
    await supervisor.start_worker("maps_stale_sweep", _maps_stale_verification_loop)
    await supervisor.start_worker("places_cache_cleanup", _places_cache_cleanup_loop)
    await supervisor.start_worker("maps_retry_sweep", _maps_retry_verification_loop)
    logger.info("Platform supervisor: all API-owned workers started.")
    try:
        yield
    finally:
        await supervisor.graceful_shutdown()


async def _maps_stale_verification_loop():
    from app.core.database import AsyncSessionLocal
    import sqlalchemy as sa
    import random
    while True:
        sleep_secs = 24 * 3600 + random.randint(-1800, 1800)
        await asyncio.sleep(sleep_secs)
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(sa.text("""
                    UPDATE organization_addresses
                    SET maps_verification_status = 'stale'
                    WHERE maps_verification_status = 'verified'
                      AND maps_last_verified_at < now() - interval '30 days'
                      AND deleted_at IS NULL
                """))
                await db.commit()
                logger.info("Maps stale sweep completed.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Maps stale sweep error: %s", exc)


async def _places_cache_cleanup_loop():
    from app.core.database import AsyncSessionLocal
    import sqlalchemy as sa
    import random
    while True:
        sleep_secs = 24 * 3600 + random.randint(-1800, 1800)
        await asyncio.sleep(sleep_secs)
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(sa.text("""
                    DELETE FROM google_places_cache
                    WHERE expires_at < now() - interval '90 days'
                """))
                await db.commit()
                if result.rowcount:
                    logger.info("Cleaned %d expired places cache entries.", result.rowcount)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Places cache cleanup error: %s", exc)


async def _maps_retry_verification_loop():
    from app.core.database import AsyncSessionLocal
    import sqlalchemy as sa
    from app.tasks.geocoding import geocode_address_task
    while True:
        await asyncio.sleep(300)
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(sa.text("""
                    SELECT id
                    FROM organization_addresses
                    WHERE maps_verification_status IN ('pending', 'stale')
                      AND (maps_next_retry_at IS NULL OR maps_next_retry_at <= now())
                      AND maps_retry_count < 10
                      AND google_place_id IS NOT NULL
                      AND deleted_at IS NULL
                    ORDER BY maps_next_retry_at NULLS FIRST
                    LIMIT 50
                    FOR UPDATE SKIP LOCKED
                """))
                rows = result.fetchall()
                for row in rows:
                    await db.execute(sa.text("""
                        UPDATE organization_addresses
                        SET maps_next_retry_at = now() + interval '10 minutes'
                        WHERE id = :addr_id
                    """), {"addr_id": row.id})
                    geocode_address_task.delay(str(row.id))
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Maps retry verification sweep error: %s", exc)
