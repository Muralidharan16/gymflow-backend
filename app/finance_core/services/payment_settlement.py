from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class VerifiedPaymentApplicationResult:
    application_record_id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID | None
    allocation_id: uuid.UUID | None
    allocated_amount: Decimal
    unapplied_amount: Decimal
    invoice_outstanding_amount: Decimal | None
    invoice_status: str | None
    decision_code: str
    replayed: bool


class FinanceVerifiedPaymentSettlementService:
    """PAY-9 bridge from verified provider money to Finance allocation truth.

    The database capability derives the only eligible invoice from certified
    Finance/member-subscription bindings.  Callers cannot nominate an invoice,
    an amount or an entitlement target.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def apply_verified_provider_payment(
        self,
        *,
        payment_id: uuid.UUID,
        payment_event_id: uuid.UUID,
    ) -> VerifiedPaymentApplicationResult:
        result = await self._session.execute(
            text(
                """
                SELECT
                    application_record_id,
                    payment_id,
                    invoice_id,
                    allocation_id,
                    allocated_amount,
                    unapplied_amount,
                    invoice_outstanding_amount,
                    invoice_status,
                    decision_code,
                    replayed
                FROM app_secure.apply_verified_provider_payment(
                    :payment_id,
                    :payment_event_id
                )
                """
            ),
            {
                "payment_id": payment_id,
                "payment_event_id": payment_event_id,
            },
        )
        return VerifiedPaymentApplicationResult(
            **dict(result.mappings().one())
        )
