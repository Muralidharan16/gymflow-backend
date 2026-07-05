import re
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.member import Member
from app.models.enums import MemberStatus
from app.models.member_subscription_v2 import (
    MemberSubscriptionV2,
    ModernSubscriptionStatus,
    SubscriptionMember,
    SubscriptionMemberRole,
)
from app.models.membership_plan import MembershipPlan, PlanStatus
from app.models.org_branch import OrgBranch
from app.models.organization import Organization
from app.repositories.member_subscription_v2_repo import MemberSubscriptionV2Repository
from app.schemas.member_subscription_v2 import SubscriptionCreate
from app.utils.subscription_dates import calculate_subscription_end_date


def _clean_org_prefix(slug: str | None) -> str:
    if not slug:
        return "ORG"
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", slug)[:6].upper()
    return cleaned or "ORG"


class MemberSubscriptionV2Service:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MemberSubscriptionV2Repository(session)

    async def create_subscription(
        self,
        org_id: uuid.UUID,
        data: SubscriptionCreate,
        actor_id: uuid.UUID | None,
    ) -> MemberSubscriptionV2:
        start_date = data.start_date or date.today()

        org = await self.session.get(Organization, org_id)
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

        branch = await self._load_branch(org_id, data.branch_id)
        member = await self._load_member(org_id, data.primary_member_id)
        plan = await self._load_plan(org_id, data.membership_plan_id)

        self._validate_member(member)
        self._validate_plan(plan, branch.id)

        if await self.repo.has_active_for_primary_member(org_id, member.id, start_date):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Primary member already has an active subscription",
            )

        if plan.max_members < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Plan max_members must be at least 1")

        end_date = calculate_subscription_end_date(start_date, plan.duration_value, plan.duration_unit)
        seq = await self.repo.next_subscription_sequence(org_id)
        subscription_code = f"SUB-{_clean_org_prefix(getattr(org, 'slug', None))}-{seq:03d}"

        subscription = MemberSubscriptionV2(
            org_id=org_id,
            branch_id=branch.id,
            membership_plan_id=plan.id,
            primary_member_id=member.id,
            subscription_code=subscription_code,
            start_date=start_date,
            end_date=end_date,
            status=ModernSubscriptionStatus.active,
            price_snapshot=plan.price,
            currency_code=plan.currency,
            duration_value_snapshot=plan.duration_value,
            duration_unit_snapshot=plan.duration_unit,
            max_members_snapshot=plan.max_members,
            created_by=actor_id,
            updated_by=actor_id,
        )
        subscription = await self.repo.create(subscription)

        slot = SubscriptionMember(
            org_id=org_id,
            subscription_id=subscription.id,
            member_id=member.id,
            slot_number=1,
            role=SubscriptionMemberRole.primary,
            is_active=True,
        )
        await self.repo.create_member(slot)
        subscription.members = [slot]
        return subscription

    async def list_subscriptions(
        self,
        org_id: uuid.UUID,
        page: int,
        size: int,
        status_filter: ModernSubscriptionStatus | None = None,
        branch_id: uuid.UUID | None = None,
        member_id: uuid.UUID | None = None,
    ) -> tuple[list[MemberSubscriptionV2], int]:
        subscriptions, total = await self.repo.list_by_org(org_id, page, size, status_filter, branch_id, member_id)
        members_by_subscription = await self.repo.list_members([sub.id for sub in subscriptions])
        for sub in subscriptions:
            sub.members = members_by_subscription.get(sub.id, [])
        return subscriptions, total

    async def get_subscription(self, org_id: uuid.UUID, subscription_id: uuid.UUID) -> MemberSubscriptionV2 | None:
        subscription = await self.repo.get_by_id_org(subscription_id, org_id)
        if not subscription:
            return None
        members_by_subscription = await self.repo.list_members([subscription.id])
        subscription.members = members_by_subscription.get(subscription.id, [])
        return subscription

    async def _load_branch(self, org_id: uuid.UUID, branch_id: uuid.UUID) -> OrgBranch:
        result = await self.session.execute(select(OrgBranch).where(OrgBranch.id == branch_id, OrgBranch.org_id == org_id))
        branch = result.scalar_one_or_none()
        if not branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Branch not found or does not belong to organization")
        return branch

    async def _load_member(self, org_id: uuid.UUID, member_id: uuid.UUID) -> Member:
        result = await self.session.execute(select(Member).where(Member.id == member_id, Member.org_id == org_id, Member.is_active == True))
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Member not found or does not belong to organization")
        return member

    async def _load_plan(self, org_id: uuid.UUID, plan_id: uuid.UUID) -> MembershipPlan:
        result = await self.session.execute(select(MembershipPlan).where(MembershipPlan.id == plan_id, MembershipPlan.org_id == org_id))
        plan = result.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Membership plan not found or does not belong to organization")
        return plan

    def _validate_member(self, member: Member) -> None:
        if member.status != MemberStatus.active or not member.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Member must be active")

    def _validate_plan(self, plan: MembershipPlan, branch_id: uuid.UUID) -> None:
        if plan.status != PlanStatus.active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only active membership plans can be used")
        if plan.branch_id is not None and plan.branch_id != branch_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Membership plan is not available for selected branch")
