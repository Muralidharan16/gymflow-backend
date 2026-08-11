import asyncio
import logging

from celery import shared_task

from app.core.database import (
    maintenance_async_session_maker,
    update_session_context,
)
from app.services.branch_lifecycle_service import BranchLifecycleService

logger = logging.getLogger(__name__)

_MAINTENANCE_CONTEXT = "lifecycle"


async def _prepare_maintenance_session(session) -> None:
    """Install transaction-local context required by maintenance FORCE-RLS policies."""
    await update_session_context(
        session,
        internal_maintenance=_MAINTENANCE_CONTEXT,
        role="lifecycle_maintenance",
        trace_id="lifecycle-maintenance",
    )


async def _run_watchdog_sweep() -> None:
    async with maintenance_async_session_maker() as session:
        await _prepare_maintenance_session(session)
        service = BranchLifecycleService(session)
        try:
            await service.run_watchdog_sweep()
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Lifecycle watchdog sweep failed")
            raise


async def _run_reconciliation_sweep() -> int:
    # Reconciliation already uses FOR UPDATE SKIP LOCKED plus durable claims.
    # Sleeping inside a Celery task only consumes a worker slot and does not add
    # correctness, so concurrent schedulers rely on the database claim boundary.
    async with maintenance_async_session_maker() as session:
        await _prepare_maintenance_session(session)
        service = BranchLifecycleService(session)
        try:
            synced_count = await service.run_reconciliation_sweep()
            await session.commit()
            if synced_count > 0:
                logger.info(
                    "Lifecycle reconciliation sweep synced %s branches",
                    synced_count,
                )
            return synced_count
        except Exception:
            await session.rollback()
            logger.exception("Lifecycle reconciliation sweep failed")
            raise


@shared_task(
    name="app.tasks.branch_lifecycle_sweeps.watchdog",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
def run_watchdog() -> None:
    asyncio.run(_run_watchdog_sweep())


@shared_task(
    name="app.tasks.branch_lifecycle_sweeps.reconciliation",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
def run_reconciliation() -> int:
    return asyncio.run(_run_reconciliation_sweep())
