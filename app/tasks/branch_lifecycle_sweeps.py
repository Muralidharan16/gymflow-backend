import asyncio
import logging

from celery import shared_task
from sqlalchemy import text

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
    # P4B reconciliation only enqueues durable search repair work. It never
    # writes a provider-success marker itself; the leased search worker owns
    # downstream execution and evidence acknowledgement.
    async with maintenance_async_session_maker() as session:
        await _prepare_maintenance_session(session)
        service = BranchLifecycleService(session)
        try:
            enqueued_count = await service.run_reconciliation_sweep()
            await session.commit()
            if enqueued_count > 0:
                logger.info(
                    "Lifecycle reconciliation sweep enqueued %s search repairs",
                    enqueued_count,
                )
            return enqueued_count
        except Exception:
            await session.rollback()
            logger.exception("Lifecycle reconciliation sweep failed")
            raise


async def _run_notification_reconciliation_sweep(batch_size: int = 100) -> int:
    # Global discovery stays on the maintenance identity. The resulting
    # notification.reconcile commands contain only authoritative command IDs;
    # provider access is still performed later by the ordinary worker identity.
    async with maintenance_async_session_maker() as session:
        await _prepare_maintenance_session(session)
        try:
            enqueued_count = int(
                await session.scalar(
                    text(
                        """
                        SELECT app_secure.enqueue_notification_reconciliation(
                            CAST(:batch_size AS integer)
                        )
                        """
                    ),
                    {"batch_size": batch_size},
                )
                or 0
            )
            await session.commit()
            if enqueued_count:
                logger.info(
                    "Notification reconciliation sweep enqueued %s provider checks",
                    enqueued_count,
                )
            return enqueued_count
        except Exception:
            await session.rollback()
            logger.exception("Notification reconciliation sweep failed")
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


@shared_task(
    name="app.tasks.branch_lifecycle_sweeps.notification_reconciliation",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
def run_notification_reconciliation() -> int:
    return asyncio.run(_run_notification_reconciliation_sweep())
