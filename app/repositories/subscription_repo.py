from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.subscription import MemberSubscription, SubscriptionStatus, SubscriptionPlan
from app.repositories.base import BaseRepository


class SubscriptionRepository(BaseRepository[MemberSubscription]):
    """Repository for MemberSubscription operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(MemberSubscription, session)

    # === Core CRUD ===
    async def get_by_id(self, sub_id: UUID, gym_id: UUID) -> Optional[MemberSubscription]:
        """
        Get subscription by ID, scoped to gym.
        
        Args:
            sub_id: Subscription UUID
            gym_id: Gym UUID for access control
        """
        query = select(MemberSubscription).where(
            MemberSubscription.id == sub_id,
            MemberSubscription.gym_id == gym_id
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan),
            selectinload(MemberSubscription.gym)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, subscription: MemberSubscription) -> MemberSubscription:
        """Create a new subscription."""
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def update(self, subscription: MemberSubscription) -> MemberSubscription:
        """Update an existing subscription."""
        await self.session.merge(subscription)
        await self.session.flush()
        return subscription

    # === Queries for member history ===
    async def get_history_for_member(self, member_id: UUID, gym_id: UUID) -> List[MemberSubscription]:
        """Get all subscriptions for a member (history), ordered by start_date DESC."""
        query = select(MemberSubscription).where(
            MemberSubscription.member_id == member_id,
            MemberSubscription.gym_id == gym_id
        ).options(
            selectinload(MemberSubscription.plan)
        ).order_by(MemberSubscription.start_date.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_for_member(self, member_id: UUID, gym_id: UUID) -> Optional[MemberSubscription]:
        """Get current active subscription for a member."""
        query = select(MemberSubscription).where(
            MemberSubscription.member_id == member_id,
            MemberSubscription.gym_id == gym_id,
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date >= date.today()
        ).options(
            selectinload(MemberSubscription.plan)
        ).order_by(MemberSubscription.end_date).limit(1)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def has_active_subscription(self, member_id: UUID, gym_id: UUID) -> bool:
        """Check if member has any active subscription."""
        query = select(func.count()).select_from(MemberSubscription).where(
            MemberSubscription.member_id == member_id,
            MemberSubscription.gym_id == gym_id,
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date >= date.today()
        )
        result = await self.session.execute(query)
        return result.scalar() > 0

    # === Expiry-related queries ===
    async def get_expired_active_subscriptions(self, cutoff_date: date) -> List[MemberSubscription]:
        """
        Get active subscriptions that have end_date < cutoff_date.
        Note: This method typically should be scoped by gym_id, but called from task that may need all gyms.
        For cross-gym tasks, no gym_id filter.
        """
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date < cutoff_date
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_expired_subscriptions_in_range(self, start_date: date, end_date: date) -> List[MemberSubscription]:
        """Get subscriptions that expired between start_date and end_date (inclusive)."""
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.EXPIRED,
            MemberSubscription.end_date >= start_date,
            MemberSubscription.end_date <= end_date
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_subscriptions_ending_on(self, target_date: date) -> List[MemberSubscription]:
        """Get active subscriptions that end exactly on target_date."""
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date == target_date
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_subscriptions_ending_between(self, start_date: date, end_date: date) -> List[MemberSubscription]:
        """Get active subscriptions ending between start_date and end_date (inclusive)."""
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date >= start_date,
            MemberSubscription.end_date <= end_date
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    # === Frozen subscriptions ===
    async def get_frozen_subscriptions_ending_on(self, target_date: date) -> List[MemberSubscription]:
        """Get frozen subscriptions that end on target_date."""
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.FROZEN,
            MemberSubscription.end_date == target_date
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_frozen_subscriptions(self) -> List[MemberSubscription]:
        """Get all frozen subscriptions that are still within end_date."""
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.FROZEN,
            MemberSubscription.end_date >= date.today()
        ).options(
            selectinload(MemberSubscription.member),
            selectinload(MemberSubscription.plan)
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    # === Plan methods ===
    async def get_plan_by_id(self, plan_id: UUID) -> Optional[SubscriptionPlan]:
        """Get subscription plan by ID."""
        return await self.session.get(SubscriptionPlan, plan_id)

    async def get_plan_by_id_and_gym(self, plan_id: UUID, gym_id: UUID) -> Optional[SubscriptionPlan]:
        """Get plan by ID scoped to gym."""
        query = select(SubscriptionPlan).where(
            SubscriptionPlan.id == plan_id,
            SubscriptionPlan.gym_id == gym_id
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create_plan(self, plan: SubscriptionPlan) -> SubscriptionPlan:
        """Create a new subscription plan."""
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def update_plan(self, plan: SubscriptionPlan) -> SubscriptionPlan:
        """Update an existing plan."""
        await self.session.merge(plan)
        await self.session.flush()
        return plan

    async def get_all_plans_for_gym(self, gym_id: UUID, active_only: bool = True) -> List[SubscriptionPlan]:
        """Get all plans for a gym."""
        query = select(SubscriptionPlan).where(SubscriptionPlan.gym_id == gym_id)
        if active_only:
            query = query.where(SubscriptionPlan.is_active == True)
        query = query.order_by(SubscriptionPlan.price)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_plans_by_ids(self, plan_ids: List[UUID]) -> List[SubscriptionPlan]:
        """Bulk fetch plans by IDs."""
        query = select(SubscriptionPlan).where(SubscriptionPlan.id.in_(plan_ids))
        result = await self.session.execute(query)
        return result.scalars().all()

    # === Analytics ===
    async def count_active_subscriptions(self, gym_id: UUID) -> int:
        """Count currently active subscriptions in a gym."""
        query = select(func.count()).select_from(MemberSubscription).where(
            MemberSubscription.gym_id == gym_id,
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date >= date.today()
        )
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def count_expired_in_month(self, gym_id: UUID, year: int, month: int) -> int:
        """Count subscriptions that expired in a specific month."""
        # Convert to date range
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)
        
        query = select(func.count()).select_from(MemberSubscription).where(
            MemberSubscription.gym_id == gym_id,
            MemberSubscription.status == SubscriptionStatus.EXPIRED,
            MemberSubscription.end_date >= start_date,
            MemberSubscription.end_date <= end_date
        )
        result = await self.session.execute(query)
        return result.scalar() or 0