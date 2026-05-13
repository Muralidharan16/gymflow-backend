# app/services/onboarding_service.py
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.auth import Owner
from app.models.organization import Organization
from app.models.trial import TrialSubscription
from app.models.audit import AuditLog
from app.schemas.onboarding import OnboardingCompleteRequest
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

class OnboardingService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def complete_onboarding(
        self, 
        owner_id: str, 
        data: OnboardingCompleteRequest,
        ip_address: str,
        user_agent: str
    ) -> dict:
        """
        Finalizes the onboarding process:
        1. Updates Organization with address details.
        2. Sets Owner as onboarding_completed.
        3. Starts 7-day Free Trial.
        4. Logs audit event.
        """
        # 1. Fetch Owner and Org
        q = select(Owner).where(Owner.id == owner_id)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()
        
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")
        
        if owner.onboarding_completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Onboarding already completed"
            )

        q_org = select(Organization).where(Organization.id == owner.org_id)
        result_org = await self.session.execute(q_org)
        org = result_org.scalar_one_or_none()

        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # 2. Atomic Transaction
        try:
            async with self.session.begin_nested():
                # a. Update Organization
                org.phone = data.phone
                org.address_line1 = data.address_line1
                org.address_line2 = data.address_line2
                org.city = data.city
                org.state = data.state
                org.pincode = data.pincode
                org.profile_completed = True

                # b. Update Owner
                owner.onboarding_completed = True
                owner.onboarding_completed_at = datetime.now(timezone.utc)

                # c. Initialize Free Trial (Asia/Kolkata)
                now_ist = datetime.now(IST)
                trial_start = now_ist
                trial_end = trial_start + timedelta(days=7)
                grace_end = trial_start + timedelta(days=10)
                hard_lock_at = trial_start + timedelta(days=11)

                trial = TrialSubscription(
                    organization_id=org.id,
                    trial_start=trial_start,
                    trial_end=trial_end,
                    grace_end=grace_end,
                    hard_lock_at=hard_lock_at,
                    status="active"
                )
                self.session.add(trial)

                # d. Audit Log
                audit = AuditLog(
                    user_id=owner.id,
                    organization_id=org.id,
                    action="ONBOARDING_COMPLETED",
                    ip_address=ip_address,
                    user_agent=user_agent,
                    metadata_json={
                        "pincode": data.pincode,
                        "trial_end": trial_end.isoformat()
                    }
                )
                self.session.add(audit)

            await self.session.commit()
            
            return {
                "status": "success",
                "trial_start": trial_start.isoformat(),
                "trial_end": trial_end.isoformat(),
                "days_remaining": 7
            }

        except Exception:
            logger.exception("Failed to complete onboarding for owner %s", owner_id)
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to finalize onboarding. Please try again."
            )

    async def get_status(self, owner_id: str) -> dict:
        """
        Returns the onboarding and trial status for the current owner.
        """
        q = select(Owner).where(Owner.id == owner_id)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()
        
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")

        trial_q = select(TrialSubscription).where(TrialSubscription.organization_id == owner.org_id)
        trial_result = await self.session.execute(trial_q)
        trial = trial_result.scalar_one_or_none()

        now_ist = datetime.now(IST)
        days_remaining = 0
        if trial:
            days_remaining = max(0, (trial.trial_end.astimezone(IST).date() - now_ist.date()).days)

        return {
            "onboarding_completed": owner.onboarding_completed,
            "trial_status": trial.status if trial else "none",
            "days_remaining": days_remaining,
            "soft_lock_at": trial.trial_end.isoformat() if trial else None,
            "hard_lock_at": trial.hard_lock_at.isoformat() if trial else None
        }
