from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.read_models import AuditEventRead
from app.platform_billing.models.audit import PlatformBillingAuditEvent
from app.platform_billing.repositories.mappers import audit_event_to_read


class PlatformBillingAuditReadRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEventRead]:
        bounded_limit = min(max(limit, 1), 500)
        result = await self._session.execute(
            select(PlatformBillingAuditEvent)
            .where(PlatformBillingAuditEvent.organization_id == organization_id)
            .order_by(PlatformBillingAuditEvent.recorded_at.desc(), PlatformBillingAuditEvent.id.desc())
            .limit(bounded_limit)
            .offset(max(offset, 0))
        )
        return [audit_event_to_read(row) for row in result.scalars().all()]
