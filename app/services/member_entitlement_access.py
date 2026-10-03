from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemberEntitlementAccessService:
    """Read-only PAY-24-C canonical member-access gate."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def is_active(
        self,
        *,
        organization_id: uuid.UUID,
        member_id: uuid.UUID,
    ) -> bool:
        value = await self._session.scalar(
            text(
                """
                SELECT app_secure.member_entitlement_access_active(
                    CAST(:organization_id AS uuid),
                    CAST(:member_id AS uuid)
                )
                """
            ),
            {
                "organization_id": organization_id,
                "member_id": member_id,
            },
        )
        return bool(value)
