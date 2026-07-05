from datetime import datetime, date
from typing import List, Optional, Tuple
from uuid import UUID

import uuid

from sqlalchemy import select, func, or_, and_, update as sql_update, text, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.member import Member, MemberMeasurement, MemberStatus
from app.models.member_subscription_v2 import MemberSubscriptionV2, ModernSubscriptionStatus
from app.models.org_branch import OrgBranch
from app.models.subscription import MemberSubscription, SubscriptionStatus
from app.repositories.base import BaseRepository


class MemberRepository(BaseRepository[Member]):
    """Repository for Member operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Member, session)

    # Core CRUD methods
    async def get_by_id(self, member_id: UUID, gym_id: UUID) -> Optional[Member]:
        """Get member by ID scoped to gym."""
        query = select(Member).where(
            Member.id == member_id,
            Member.gym_id == gym_id,
            Member.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id_org(self, member_id: UUID, org_id: UUID) -> Optional[Member]:
        """Get member by ID scoped to org."""
        query = select(Member).where(
            Member.id == member_id,
            Member.org_id == org_id,
            Member.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id_active(self, member_id: UUID, gym_id: UUID) -> Optional[Member]:
        """Get active member by ID and gym."""
        query = select(Member).where(
            Member.id == member_id,
            Member.gym_id == gym_id,
            Member.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_uid_active(self, member_uid: str) -> Optional[Member]:
        """
        Get active member by UID (unique identifier across org).
        Used for QR code check-in.
        """
        query = select(Member).where(
            Member.member_uid == member_uid,
            Member.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str, gym_id: UUID) -> Optional[Member]:
        """Get member by normalized phone number and gym."""
        query = select(Member).where(
            Member.phone == phone,
            Member.gym_id == gym_id
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_phone_org(self, phone: str, org_id: UUID) -> Optional[Member]:
        """Get member by normalized phone number and org."""
        query = select(Member).where(
            Member.phone == phone,
            Member.org_id == org_id,
            Member.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, member: Member) -> Member:
        """Create a new member."""
        self.session.add(member)
        await self.session.flush()
        return member

    async def next_member_number(self, org_id: UUID) -> int:
        """Atomically allocate the next organization-scoped member number."""
        query = text("""
            INSERT INTO organization_counters (id, org_id, counter_key, current_value)
            VALUES (:id, :org_id, 'member', 100)
            ON CONFLICT (org_id, counter_key)
            DO UPDATE SET
                current_value = organization_counters.current_value + 1,
                updated_at = now()
            RETURNING current_value;
        """)
        result = await self.session.execute(query, {"id": uuid.uuid4(), "org_id": org_id})
        return result.scalar_one()

    async def update(self, member: Member) -> Member:
        """Update an existing member."""
        await self.session.merge(member)
        await self.session.flush()
        return member

    async def soft_delete(self, member_id: UUID, gym_id: UUID) -> bool:
        """
        Soft delete a member (set is_active=False).
        
        Args:
            member_id: Member UUID
            gym_id: Gym UUID for scoping
            
        Returns:
            True if deleted, False if not found
        """
        query = sql_update(Member).where(
            Member.id == member_id,
            Member.gym_id == gym_id,
            Member.is_active == True
        ).values(is_active=False)
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    async def soft_delete_org(self, member_id: UUID, org_id: UUID) -> bool:
        """
        Soft delete a member (set is_active=False) scoped to org.
        """
        query = sql_update(Member).where(
            Member.id == member_id,
            Member.org_id == org_id,
            Member.is_active == True
        ).values(is_active=False)
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    # Search and listing methods
    async def search(
        self,
        gym_id: UUID,
        status: Optional[MemberStatus] = None,
        search_term: Optional[str] = None,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[Member], int]:
        """
        Search members with pagination.
        
        Args:
            gym_id: Gym UUID
            status: Filter by member status
            search_term: Search in name or phone
            page: Page number (1-indexed)
            size: Items per page
            
        Returns:
            Tuple of (list of members, total count)
        """
        offset = (page - 1) * size
        
        # Base query
        query = select(Member).where(
            Member.gym_id == gym_id,
            Member.is_active == True
        )
        
        # Apply filters
        if status:
            query = query.where(Member.status == status)
        
        if search_term:
            search_pattern = f"%{search_term}%"
            query = query.where(
                or_(
                    Member.name.ilike(search_pattern),
                    Member.phone.contains(search_term)
                )
            )
        
        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        
        # Get paginated results
        query = query.order_by(Member.name).offset(offset).limit(size)
        result = await self.session.execute(query)
        members = result.scalars().all()
        
        return members, total

    async def search_org(
        self,
        org_id: UUID,
        home_branch_id: Optional[UUID] = None,
        status: Optional[MemberStatus] = None,
        search_term: Optional[str] = None,
        is_active: bool = True,
        has_active_subscription: Optional[bool] = None,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[Member], int]:
        """Search members with pagination scoped to org."""
        offset = (page - 1) * size

        active_subscription_exists = exists(
            select(1).where(
                MemberSubscriptionV2.org_id == org_id,
                MemberSubscriptionV2.primary_member_id == Member.id,
                MemberSubscriptionV2.status == ModernSubscriptionStatus.active,
            )
        )

        filters = [
            Member.org_id == org_id,
            Member.is_active == is_active
        ]
        
        if home_branch_id:
            filters.append(Member.home_branch_id == home_branch_id)
            
        if status:
            filters.append(Member.status == status)

        if has_active_subscription is not None:
            filters.append(active_subscription_exists if has_active_subscription else ~active_subscription_exists)
        
        if search_term:
            normalized = search_term.strip()
            search_pattern = f"%{normalized.lower()}%"
            search_filters = [
                func.lower(Member.name).like(search_pattern),
                Member.phone.contains(normalized),
            ]
            if normalized.isdigit():
                search_filters.append(Member.member_number == int(normalized))
            filters.append(
                or_(
                    *search_filters
                )
            )
        
        count_query = select(func.count()).select_from(Member).where(*filters)
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        active_subscription_subquery = (
            select(
                MemberSubscriptionV2.primary_member_id.label("member_id"),
                MemberSubscriptionV2.id.label("active_subscription_id"),
            )
            .where(
                MemberSubscriptionV2.org_id == org_id,
                MemberSubscriptionV2.status == ModernSubscriptionStatus.active,
            )
            .subquery()
        )

        query = (
            select(
                Member,
                OrgBranch.branch_name.label("home_branch_name"),
                active_subscription_subquery.c.active_subscription_id,
            )
            .outerjoin(OrgBranch, OrgBranch.id == Member.home_branch_id)
            .outerjoin(active_subscription_subquery, active_subscription_subquery.c.member_id == Member.id)
            .where(*filters)
            .order_by(Member.member_number, Member.name)
            .offset(offset)
            .limit(size)
        )
        result = await self.session.execute(query)
        members: list[Member] = []
        for member, home_branch_name, active_subscription_id in result.all():
            member.home_branch_name = home_branch_name
            member.active_subscription_id = active_subscription_id
            member.has_active_subscription = active_subscription_id is not None
            members.append(member)
        
        return members, total

    async def get_all_for_gym(self, gym_id: UUID, is_active: bool = True) -> List[Member]:
        """Get all members for a gym."""
        query = select(Member).where(
            Member.gym_id == gym_id,
            Member.is_active == is_active
        ).order_by(Member.name)
        result = await self.session.execute(query)
        return result.scalars().all()

    # Analytics and reporting methods
    async def get_inactive_members_with_active_subscription(self, since_date: datetime) -> List[Member]:
        """
        Get members who haven't checked in since_date but have an active subscription.
        Used for inactivity reminders.
        """
        query = select(Member).where(
            Member.is_active == True,
            Member.last_check_in < since_date,
            Member.id.in_(
                select(MemberSubscription.member_id).where(
                    MemberSubscription.status == SubscriptionStatus.ACTIVE,
                    MemberSubscription.end_date >= date.today()
                )
            )
        ).options(selectinload(Member.subscriptions))
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_members_with_birthday(self, target_date: date) -> List[Member]:
        """
        Get members whose birthday matches target_date (month and day).
        """
        query = select(Member).where(
            Member.is_active == True,
            Member.date_of_birth.isnot(None),
            func.extract('month', Member.date_of_birth) == target_date.month,
            func.extract('day', Member.date_of_birth) == target_date.day
        )
        result = await self.session.execute(query)
        return result.scalars().all()

    async def count_active_members(self, gym_id: UUID) -> int:
        """Count members with active subscriptions in a gym."""
        query = select(func.count(Member.id.distinct())).select_from(Member).join(
            MemberSubscription, Member.id == MemberSubscription.member_id
        ).where(
            Member.gym_id == gym_id,
            Member.is_active == True,
            MemberSubscription.status == SubscriptionStatus.ACTIVE
        )
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def count_new_members(self, gym_id: UUID, from_date: date) -> int:
        """Count members created after from_date."""
        query = select(func.count(Member.id)).where(
            Member.gym_id == gym_id,
            Member.is_active == True,
            Member.created_at >= from_date
        )
        result = await self.session.execute(query)
        return result.scalar() or 0

    # Measurement methods
    async def create_measurement(self, measurement: MemberMeasurement) -> MemberMeasurement:
        """Create a new member measurement log."""
        self.session.add(measurement)
        await self.session.flush()
        return measurement

    async def get_measurements(self, member_id: UUID, gym_id: UUID) -> List[MemberMeasurement]:
        """
        Get measurement history for a member.
        Returns ordered by measured_on DESC.
        """
        query = select(MemberMeasurement).where(
            MemberMeasurement.member_id == member_id,
            MemberMeasurement.gym_id == gym_id
        ).order_by(MemberMeasurement.measured_on.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_latest_measurement(self, member_id: UUID, gym_id: UUID) -> Optional[MemberMeasurement]:
        """Get the most recent measurement for a member."""
        query = select(MemberMeasurement).where(
            MemberMeasurement.member_id == member_id,
            MemberMeasurement.gym_id == gym_id
        ).order_by(MemberMeasurement.measured_on.desc()).limit(1)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
