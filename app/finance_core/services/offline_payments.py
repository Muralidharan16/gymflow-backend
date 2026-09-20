from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PreparedOfflinePayment:
    offline_payment_request_id: uuid.UUID
    invoice_id: uuid.UUID
    status: str
    amount: Decimal
    currency_code: str
    payment_method: str
    reference_code: str
    prepared_actor_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True)
class ApprovedOfflinePayment:
    offline_payment_request_id: uuid.UUID
    payment_id: uuid.UUID
    allocation_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_status: str
    status: str
    replayed: bool


@dataclass(frozen=True)
class RejectedOfflinePayment:
    offline_payment_request_id: uuid.UUID
    status: str
    rejection_reason_code: str
    replayed: bool


class FinanceOfflinePaymentService:
    def __init__(self, session: AsyncSession):
        self._session=session

    async def prepare(
        self,
        *,
        invoice_id: uuid.UUID,
        payment_method: str,
        amount: Decimal,
        currency_code: str,
        reference_code: str,
        proof_sha256: str,
        idempotency_key: str,
    ) -> PreparedOfflinePayment:
        result=await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.prepare_offline_payment(
                    :invoice_id,
                    :payment_method,
                    :amount,
                    :currency_code,
                    :reference_code,
                    :proof_sha256,
                    :idempotency_key
                )
                """
            ),
            {
                "invoice_id":invoice_id,
                "payment_method":payment_method,
                "amount":amount,
                "currency_code":currency_code,
                "reference_code":reference_code,
                "proof_sha256":proof_sha256,
                "idempotency_key":idempotency_key,
            },
        )
        row=result.mappings().one()
        return PreparedOfflinePayment(**dict(row))

    async def approve(
        self,
        *,
        offline_payment_request_id: uuid.UUID,
        idempotency_key: str,
    ) -> ApprovedOfflinePayment:
        result=await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.approve_offline_payment(
                    :offline_payment_request_id,
                    :idempotency_key
                )
                """
            ),
            {
                "offline_payment_request_id":offline_payment_request_id,
                "idempotency_key":idempotency_key,
            },
        )
        return ApprovedOfflinePayment(**dict(result.mappings().one()))

    async def reject(
        self,
        *,
        offline_payment_request_id: uuid.UUID,
        reason_code: str,
        idempotency_key: str,
    ) -> RejectedOfflinePayment:
        result=await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.reject_offline_payment(
                    :offline_payment_request_id,
                    :reason_code,
                    :idempotency_key
                )
                """
            ),
            {
                "offline_payment_request_id":offline_payment_request_id,
                "reason_code":reason_code,
                "idempotency_key":idempotency_key,
            },
        )
        return RejectedOfflinePayment(**dict(result.mappings().one()))
