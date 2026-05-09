import uuid
from typing import Optional, List
from datetime import date
from sqlalchemy import select, func
from app.models.subscription import SubscriptionPlan, MemberSubscription, MemberFreezeLog
from app.models.enums import SubscriptionStatus
from app.repositories.base import BaseRepository

class PlanRepository(BaseRepository[SubscriptionPlan]):
    def __init__(self, session):
        super().__init__(SubscriptionPlan, session)

    async def list_active(self, gym_id: uuid.UUID) -> List[SubscriptionPlan]:
        """Tenant-safe fetch of active plans."""
        q = select(self.model).where(
            self.model.gym_id == gym_id,
            self.model.is_active == True
        )
        result = await self.session.execute(q)
        return list(result.scalars().all())

class SubscriptionRepository(BaseRepository[MemberSubscription]):
    def __init__(self, session):
        super().__init__(MemberSubscription, session)

    async def get_active_for_member(
        self, 
        member_id: uuid.UUID, 
        gym_id: uuid.UUID
    ) -> Optional[MemberSubscription]:
        """Tenant-safe fetch of active subscription for a member."""
        q = select(self.model).where(
            self.model.member_id == member_id,
            self.model.gym_id == gym_id,
            self.model.status == SubscriptionStatus.active
        ).order_by(self.model.end_date.desc())
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def get_expiring_in_days(self, gym_id: uuid.UUID, days: int) -> List[MemberSubscription]:
        """Fetch subscriptions expiring in exactly X days for reminders."""
        q = select(self.model).where(
            self.model.gym_id == gym_id,
            self.model.status == SubscriptionStatus.active,
            self.model.end_date == func.current_date() + days
        )
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def count_active_in_org(self, org_id: uuid.UUID) -> int:
        """Org-safe count of all active subscriptions across branches."""
        from app.models.member import Member
        q = select(func.count(self.model.id)).join(Member).where(
            Member.org_id == org_id,
            self.model.status == SubscriptionStatus.active
        )
        result = await self.session.execute(q)
        return await result.scalar_one()

class FreezeLogRepository(BaseRepository[MemberFreezeLog]):
    def __init__(self, session):
        super().__init__(MemberFreezeLog, session)

    async def get_active_for_subscription(
        self, 
        subscription_id: uuid.UUID,
        gym_id: uuid.UUID
    ) -> Optional[MemberFreezeLog]:
        """Tenant-safe fetch of an active freeze period."""
        from app.models.enums import FreezeStatus
        q = select(self.model).where(
            self.model.subscription_id == subscription_id,
            self.model.gym_id == gym_id,
            self.model.status == FreezeStatus.active
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()
