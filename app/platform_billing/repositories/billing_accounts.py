from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.read_models import BillingAccountRead
from app.platform_billing.models.billing_account import PlatformBillingAccount
from app.platform_billing.repositories.mappers import billing_account_to_read


class PlatformBillingAccountReadRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_active_for_organization(self, organization_id: uuid.UUID) -> BillingAccountRead | None:
        result = await self._session.execute(
            select(PlatformBillingAccount).where(
                PlatformBillingAccount.organization_id == organization_id,
                PlatformBillingAccount.status == "active",
            )
        )
        row = result.scalar_one_or_none()
        return billing_account_to_read(row) if row is not None else None

    async def get_by_id_for_organization(
        self,
        *,
        organization_id: uuid.UUID,
        billing_account_id: uuid.UUID,
    ) -> BillingAccountRead | None:
        result = await self._session.execute(
            select(PlatformBillingAccount).where(
                PlatformBillingAccount.organization_id == organization_id,
                PlatformBillingAccount.id == billing_account_id,
            )
        )
        row = result.scalar_one_or_none()
        return billing_account_to_read(row) if row is not None else None
