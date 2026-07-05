import asyncio
import logging
import random
from celery import shared_task

from app.core.database import async_session_maker
from app.services.branch_lifecycle_service import BranchLifecycleService

logger = logging.getLogger(__name__)

async def _run_watchdog_sweep():
    async with async_session_maker() as session:
        service = BranchLifecycleService(session)
        try:
            await service.run_watchdog_sweep()
            await session.commit()
        except Exception as e:
            logger.error(f"Error in watchdog sweep: {e}")
            await session.rollback()

async def _run_reconciliation_sweep():
    # Anti-thundering herd: jitter before starting the sweep
    await asyncio.sleep(random.uniform(0, 300))
    async with async_session_maker() as session:
        service = BranchLifecycleService(session)
        try:
            synced_count = await service.run_reconciliation_sweep()
            if synced_count > 0:
                logger.info(f"Reconciliation sweep synced {synced_count} branches.")
            await session.commit()
        except Exception as e:
            logger.error(f"Error in reconciliation sweep: {e}")
            await session.rollback()

@shared_task(name="app.tasks.branch_lifecycle_sweeps.watchdog")
def run_watchdog():
    asyncio.run(_run_watchdog_sweep())

@shared_task(name="app.tasks.branch_lifecycle_sweeps.reconciliation")
def run_reconciliation():
    asyncio.run(_run_reconciliation_sweep())
