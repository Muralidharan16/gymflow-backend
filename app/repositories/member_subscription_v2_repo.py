import uuid
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.member_subscription_v2 import MemberSubscriptionV2, ModernSubscriptionStatus, SubscriptionMember


class MemberSubscriptionV2Repository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def next_subscription_sequence(self, org_id: uuid.UUID) -> int:
        query = text("""
            INSERT INTO organization_counters (id, org_id, counter_key, current_value)
            VALUES (:id, :org_id, 'member_subscription', 1)
            ON CONFLICT (org_id, counter_key)
            DO UPDATE SET current_value = organization_counters.current_value + 1
            RETURNING current_value;
        """)
        result = await self.session.execute(query, {"id": uuid.uuid4(), "org_id": org_id})
        return result.scalar_one()

    async def create(self, subscription: MemberSubscriptionV2) -> MemberSubscriptionV2:
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def create_member(self, subscription_member: SubscriptionMember) -> SubscriptionMember:
        self.session.add(subscription_member)
        await self.session.flush()
        return subscription_member

    async def get_by_id_org(self, subscription_id: uuid.UUID, org_id: uuid.UUID) -> MemberSubscriptionV2 | None:
        result = await self.session.execute(
            select(MemberSubscriptionV2).where(
                MemberSubscriptionV2.id == subscription_id,
                MemberSubscriptionV2.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_org(
        self,
        org_id: uuid.UUID,
        page: int = 1,
        size: int = 50,
        status: ModernSubscriptionStatus | None = None,
        branch_id: uuid.UUID | None = None,
        member_id: uuid.UUID | None = None,
    ) -> tuple[list[MemberSubscriptionV2], int]:
        filters = [MemberSubscriptionV2.org_id == org_id]
        if status:
            filters.append(MemberSubscriptionV2.status == status)
        if branch_id:
            filters.append(MemberSubscriptionV2.branch_id == branch_id)
        if member_id:
            filters.append(MemberSubscriptionV2.primary_member_id == member_id)

        count_result = await self.session.execute(
            select(func.count()).select_from(MemberSubscriptionV2).where(*filters)
        )
        total = count_result.scalar() or 0

        result = await self.session.execute(
            select(MemberSubscriptionV2)
            .where(*filters)
            .order_by(MemberSubscriptionV2.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(result.scalars().all()), total

    async def list_members(self, subscription_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SubscriptionMember]]:
        if not subscription_ids:
            return {}
        result = await self.session.execute(
            select(SubscriptionMember)
            .where(SubscriptionMember.subscription_id.in_(subscription_ids))
            .order_by(SubscriptionMember.slot_number)
        )
        grouped: dict[uuid.UUID, list[SubscriptionMember]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.subscription_id, []).append(row)
        return grouped

    async def has_active_for_primary_member(self, org_id: uuid.UUID, member_id: uuid.UUID, start_date: date) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(MemberSubscriptionV2)
            .where(
                MemberSubscriptionV2.org_id == org_id,
                MemberSubscriptionV2.primary_member_id == member_id,
                MemberSubscriptionV2.status.in_([ModernSubscriptionStatus.active, ModernSubscriptionStatus.frozen]),
                MemberSubscriptionV2.end_date > start_date,
            )
        )
        return bool(result.scalar() or 0)
