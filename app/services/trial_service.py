# app/services/trial_service.py
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.trial import TrialSubscription
from app.core.redis import get_redis_utils
from app.core.config import settings
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

class TrialService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.redis_utils = get_redis_utils()
        self.client = self.redis_utils.client

    async def get_trial_status(self, org_id: str) -> dict:
        """
        Get trial status with 5-minute Redis caching as per spec.
        """
        cache_key = f"trial:{org_id}"
        
        # 1. Try cache
        try:
            cached = await self.client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            logger.warning("Redis failure fetching trial status for %s", org_id)

        # 2. Fetch from DB
        q = select(TrialSubscription).where(TrialSubscription.organization_id == org_id)
        result = await self.session.execute(q)
        trial = result.scalar_one_or_none()

        if not trial:
            return {"status": "none", "is_hard_locked": False, "is_soft_locked": False}

        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc.astimezone(IST)  # display-only — not used in canonical writes
        
        # Determine effective status based on dates (fallback if scheduler hasn't run)
        # Compare UTC against UTC-aware DB timestamps directly — no timezone conversion needed
        is_hard_locked = trial.status == "hard_locked" or now_utc > trial.hard_lock_at
        is_soft_locked = trial.status == "soft_locked" or (not is_hard_locked and now_utc > trial.trial_end)
        
        status_data = {
            "status": trial.status,
            "is_hard_locked": is_hard_locked,
            "is_soft_locked": is_soft_locked,
            "trial_end": trial.trial_end.isoformat(),
            "hard_lock_at": trial.hard_lock_at.isoformat(),
            "days_remaining": max(0, (trial.trial_end.astimezone(IST).date() - now_ist.date()).days)
        }

        # 3. Cache for 5 minutes
        try:
            await self.client.setex(cache_key, 300, json.dumps(status_data))
        except Exception:
            logger.warning("Failed to cache trial status for %s", org_id)

        return status_data

    async def invalidate_cache(self, org_id: str):
        await self.client.delete(f"trial:{org_id}")
