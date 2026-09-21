from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class FinanceSecurityAuditService:
    """Append high-risk Finance security evidence through the DB capability."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def record(
        self,
        *,
        event_type: str,
        target_type: str,
        target_id: uuid.UUID | None,
        reason_code: str | None,
        severity: str = "info",
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                """
                SELECT app_secure.record_finance_security_audit(
                    :event_type,
                    :target_type,
                    :target_id,
                    :reason_code,
                    :severity
                )
                """
            ),
            {
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "reason_code": reason_code,
                "severity": severity,
            },
        )
        return result.scalar_one()
