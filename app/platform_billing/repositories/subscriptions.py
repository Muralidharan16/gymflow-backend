from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.read_models import (
    SubscriptionEventRead,
    SubscriptionItemRead,
    SubscriptionPeriodRead,
    SubscriptionRead,
)
from app.platform_billing.models.subscription import (
    PlatformSubscription,
    PlatformSubscriptionEvent,
    PlatformSubscriptionItem,
    PlatformSubscriptionPeriod,
)
from app.platform_billing.repositories.mappers import (
    subscription_event_to_read,
    subscription_item_to_read,
    subscription_period_to_read,
    subscription_to_read,
)


CURRENT_SUBSCRIPTION_STATUSES = (
    "trialing",
    "active",
    "past_due",
    "pause_scheduled",
    "paused",
    "cancel_scheduled",
)


class PlatformSubscriptionReadRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_current_for_organization(self, organization_id: uuid.UUID) -> SubscriptionRead | None:
        result = await self._session.execute(
            select(PlatformSubscription)
            .where(
                PlatformSubscription.organization_id == organization_id,
                PlatformSubscription.status.in_(CURRENT_SUBSCRIPTION_STATUSES),
            )
            .order_by(PlatformSubscription.created_at.desc())
        )
        row = result.scalars().first()
        return subscription_to_read(row) if row is not None else None

    async def get_by_id_for_organization(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
    ) -> SubscriptionRead | None:
        result = await self._session.execute(
            select(PlatformSubscription).where(
                PlatformSubscription.organization_id == organization_id,
                PlatformSubscription.id == subscription_id,
            )
        )
        row = result.scalar_one_or_none()
        return subscription_to_read(row) if row is not None else None

    async def list_items(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
    ) -> list[SubscriptionItemRead]:
        result = await self._session.execute(
            select(PlatformSubscriptionItem)
            .where(
                PlatformSubscriptionItem.organization_id == organization_id,
                PlatformSubscriptionItem.subscription_id == subscription_id,
            )
            .order_by(PlatformSubscriptionItem.effective_from, PlatformSubscriptionItem.id)
        )
        return [subscription_item_to_read(row) for row in result.scalars().all()]

    async def list_periods(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
    ) -> list[SubscriptionPeriodRead]:
        result = await self._session.execute(
            select(PlatformSubscriptionPeriod)
            .where(
                PlatformSubscriptionPeriod.organization_id == organization_id,
                PlatformSubscriptionPeriod.subscription_id == subscription_id,
            )
            .order_by(PlatformSubscriptionPeriod.starts_at, PlatformSubscriptionPeriod.id)
        )
        return [subscription_period_to_read(row) for row in result.scalars().all()]

    async def list_events(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubscriptionEventRead]:
        bounded_limit = min(max(limit, 1), 500)
        result = await self._session.execute(
            select(PlatformSubscriptionEvent)
            .where(
                PlatformSubscriptionEvent.organization_id == organization_id,
                PlatformSubscriptionEvent.subscription_id == subscription_id,
            )
            .order_by(PlatformSubscriptionEvent.sequence_number, PlatformSubscriptionEvent.recorded_at)
            .limit(bounded_limit)
            .offset(max(offset, 0))
        )
        return [subscription_event_to_read(row) for row in result.scalars().all()]
