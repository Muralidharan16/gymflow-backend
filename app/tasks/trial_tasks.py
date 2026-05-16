# app/tasks/trial_tasks.py
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select, update
from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.trial import TrialSubscription
from app.models.auth import Owner
from app.models.audit import AuditLog
from app.utils.email_utils import send_trial_reminder_email # We'll need to implement this
from app.services.trial_service import TrialService
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger("doers.tasks")
IST = ZoneInfo("Asia/Kolkata")

@celery_app.task(name="app.tasks.trial_tasks.monitor_trial_lifecycles")
def monitor_trial_lifecycles():
    """
    Synchronous wrapper for the async trial monitor.
    Celery workers run this every midnight IST.
    """
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_monitor_trials())

async def _monitor_trials():
    async with AsyncSessionLocal() as session:
        now_ist = datetime.now(IST)
        today = now_ist.date()
        
        # 1. Process Soft Locks (Trials that ended yesterday)
        soft_lock_q = (
            select(TrialSubscription)
            .where(TrialSubscription.status == "active")
            .where(TrialSubscription.trial_end <= now_ist)
        )
        result = await session.execute(soft_lock_q)
        to_soft_lock = result.scalars().all()
        
        for trial in to_soft_lock:
            trial.status = "soft_locked"
            # Invalidate Redis cache via TrialService
            ts = TrialService(session)
            await ts.invalidate_cache(str(trial.organization_id))
            
            # Audit Log
            log = AuditLog(
                organization_id=trial.organization_id,
                action="TRIAL_SOFT_LOCKED",
                metadata={"reason": "trial_expired", "date": today.isoformat()}
            )
            session.add(log)
            logger.info("Soft-locked trial for org %s", trial.organization_id)

        # 2. Process Hard Locks (Grace period ended)
        hard_lock_q = (
            select(TrialSubscription)
            .where(TrialSubscription.status == "soft_locked")
            .where(TrialSubscription.hard_lock_at <= now_ist)
        )
        result = await session.execute(hard_lock_q)
        to_hard_lock = result.scalars().all()
        
        for trial in to_hard_lock:
            trial.status = "hard_locked"
            ts = TrialService(session)
            await ts.invalidate_cache(str(trial.organization_id))
            
            log = AuditLog(
                organization_id=trial.organization_id,
                action="TRIAL_HARD_LOCKED",
                metadata={"reason": "grace_period_expired", "date": today.isoformat()}
            )
            session.add(log)
            logger.info("Hard-locked trial for org %s", trial.organization_id)

        await session.commit()
        return {"soft_locked": len(to_soft_lock), "hard_locked": len(to_hard_lock)}
