from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RefundCreditNoteLinkResult:
    link_id: uuid.UUID
    refund_id: uuid.UUID
    credit_note_id: uuid.UUID
    linked_amount: Decimal
    total_backing: Decimal
    replayed: bool


@dataclass(frozen=True)
class RefundFinancialFinalizationResult:
    command_id: uuid.UUID
    refund_id: uuid.UUID
    payment_id: uuid.UUID
    ledger_entry_id: uuid.UUID
    refund_status: str
    payment_status: str
    command_status: str
    replayed: bool


class FinanceRefundFinancialFinalizationService:
    """PAY-10-D database capability wrapper.

    This service does no provider network I/O, does not issue credit notes,
    and does not commit. The database capability atomically owns refund,
    payment, ledger, and outbox finalization.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def link_issued_credit_note(
        self,
        *,
        refund_id: uuid.UUID,
        credit_note_id: uuid.UUID,
    ) -> RefundCreditNoteLinkResult:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.link_pay10_refund_credit_note(
                    :refund_id,
                    :credit_note_id
                )
                """
            ),
            {
                "refund_id": refund_id,
                "credit_note_id": credit_note_id,
            },
        )
        return RefundCreditNoteLinkResult(**dict(result.mappings().one()))

    async def finalize(
        self,
        *,
        command_id: uuid.UUID,
    ) -> RefundFinancialFinalizationResult:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.finalize_pay10_refund(:command_id)
                """
            ),
            {"command_id": command_id},
        )
        return RefundFinancialFinalizationResult(
            **dict(result.mappings().one())
        )
