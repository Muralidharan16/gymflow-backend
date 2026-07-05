from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.read_models import (
    AuditEventRead,
    BillingAccountRead,
    PlanVersionRead,
    ProductRead,
    SubscriptionEventRead,
    SubscriptionItemRead,
    SubscriptionPeriodRead,
    SubscriptionRead,
)
from app.platform_billing.repositories import (
    PlatformBillingAccountReadRepository,
    PlatformBillingAuditReadRepository,
    PlatformCatalogReadRepository,
    PlatformSubscriptionReadRepository,
)


@dataclass(frozen=True)
class SubscriptionDetailRead:
    subscription: SubscriptionRead
    items: tuple[SubscriptionItemRead, ...]
    periods: tuple[SubscriptionPeriodRead, ...]


class PlatformBillingQueryService:
    def __init__(self, session: AsyncSession):
        self._catalog = PlatformCatalogReadRepository(session)
        self._billing_accounts = PlatformBillingAccountReadRepository(session)
        self._subscriptions = PlatformSubscriptionReadRepository(session)
        self._audit = PlatformBillingAuditReadRepository(session)

    async def list_available_products(self) -> list[ProductRead]:
        return await self._catalog.list_active_products()

    async def list_published_plans(
        self,
        *,
        country_code: str | None = None,
        currency_code: str | None = None,
        now: datetime | None = None,
    ) -> list[PlanVersionRead]:
        return await self._catalog.list_published_plan_versions(
            country_code=country_code,
            currency_code=currency_code,
            now=now,
        )

    async def get_plan_detail(
        self,
        plan_version_id: uuid.UUID,
        *,
        country_code: str | None = None,
        currency_code: str | None = None,
        now: datetime | None = None,
    ) -> PlanVersionRead | None:
        return await self._catalog.get_published_plan_version(
            plan_version_id,
            country_code=country_code,
            currency_code=currency_code,
            now=now,
        )

    async def get_billing_account(self, organization_id: uuid.UUID) -> BillingAccountRead | None:
        return await self._billing_accounts.get_active_for_organization(organization_id)

    async def get_current_subscription(self, organization_id: uuid.UUID) -> SubscriptionDetailRead | None:
        subscription = await self._subscriptions.get_current_for_organization(organization_id)
        if subscription is None:
            return None
        items = await self._subscriptions.list_items(
            organization_id=organization_id,
            subscription_id=subscription.id,
        )
        periods = await self._subscriptions.list_periods(
            organization_id=organization_id,
            subscription_id=subscription.id,
        )
        return SubscriptionDetailRead(
            subscription=subscription,
            items=tuple(items),
            periods=tuple(periods),
        )

    async def list_subscription_events(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubscriptionEventRead]:
        return await self._subscriptions.list_events(
            organization_id=organization_id,
            subscription_id=subscription_id,
            limit=limit,
            offset=offset,
        )

    async def list_audit_events(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEventRead]:
        return await self._audit.list_for_organization(
            organization_id,
            limit=limit,
            offset=offset,
        )
