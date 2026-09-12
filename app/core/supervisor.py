"""
app/core/supervisor.py
=======================
Structured background task supervision tree for the Doers SaaS API process.

The FastAPI process owns only request-adjacent or process-local coroutines.
Cross-tenant/database-global maintenance is scheduled through the isolated
Celery maintenance process and must never borrow the API database identity.
P2D attests API/auth bindings before the process starts serving traffic.
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

    async def start_worker(
        self,
        name: str,
        coro_fn: Callable[..., Awaitable],
        *args,
        **kwargs,
    ) -> None:
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
                        logger.critical(
                            "Worker '%s' exceeded restart budget (%d). Giving up permanently.",
                            name,
                            self._MAX_RESTARTS,
                            exc_info=True,
                        )
                        return
                    restart_gap = min(120.0, restart_gap * 2)
                    logger.error(
                        "Worker '%s' crashed (attempt %d/%d). Restarting in %.1fs: %s",
                        name,
                        restarts,
                        self._MAX_RESTARTS,
                        restart_gap,
                        exc,
                    )
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(), timeout=restart_gap
                        )
                        return
                    except asyncio.TimeoutError:
                        pass

        task = asyncio.create_task(wrapper(), name=name)
        self.tasks.append(task)
        logger.info("Supervisor started worker: %s", name)

    async def graceful_shutdown(self, drain_timeout: float = 30.0) -> None:
        logger.info("Initiating graceful supervisor shutdown...")
        self._shutdown_event.set()
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        for name, result in zip([t.get_name() for t in self.tasks], results):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("Worker '%s' exited with error: %s", name, result)
        logger.info("All supervised workers drained. Supervisor shutdown complete.")


supervisor = BackgroundWorkerSupervisor()


async def _lock_registry_sweep_loop() -> None:
    from app.core.crypto import EnvelopeEncryptionProvider

    while True:
        await asyncio.sleep(300)
        try:
            async with asyncio.timeout(5):
                evicted = await EnvelopeEncryptionProvider._lock_registry.sweep_stale()
                if evicted:
                    logger.debug(
                        "Lock registry sweep: evicted %d stale locks.", evicted
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Lock registry sweep error: %s", exc)


async def _kms_bulkhead_sweep_loop() -> None:
    from app.core.crypto import kms_bulkhead

    while True:
        await asyncio.sleep(600)
        try:
            await kms_bulkhead.sweep_idle_breakers()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("KMS bulkhead sweep error: %s", exc)


async def _lookup_encrypted_dek(tenant_id: str, key_version: int) -> bytes:
    from app.core.database import AsyncSessionLocal, update_session_context
    import sqlalchemy as sa

    async with AsyncSessionLocal() as db:
        # This lookup owns a fresh pooled API session. Install transaction-local
        # tenant context before the first SQL; the bounded database function
        # independently verifies the same tenant before reading key material.
        await update_session_context(db, org_id=tenant_id)
        res = await db.execute(
            sa.text(
                """
                SELECT app_secure.lookup_encrypted_dek(:tid, :ver)
                """
            ),
            {"tid": tenant_id, "ver": key_version},
        )
        encrypted_dek = res.scalar_one_or_none()
        if encrypted_dek is None:
            raise ValueError(
                f"DEK not found: tenant={tenant_id} version={key_version}"
            )
        return bytes(encrypted_dek)


async def _dek_registry_startup() -> None:
    from app.core.crypto import EnvelopeEncryptionProvider

    EnvelopeEncryptionProvider.register_dek_lookup(_lookup_encrypted_dek)
    logger.info("Tenant-bound DEK registry lookup registered into EnvelopeEncryptionProvider.")


async def _wfq_start_loop() -> None:
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
    await supervisor.start_worker("lock_registry_sweep", _lock_registry_sweep_loop)
    await supervisor.start_worker("kms_bulkhead_sweep", _kms_bulkhead_sweep_loop)
    await supervisor.start_worker("wfq_dispatcher", _wfq_start_loop)
    logger.info("Platform supervisor: API-local workers started.")
    try:
        yield
    finally:
        await supervisor.graceful_shutdown()
