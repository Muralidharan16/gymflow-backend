from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.provider_boundary import (
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)


ProviderOperationOutcome = Literal[
    "succeeded",
    "failed_retryable",
    "failed_final",
    "unknown",
]


@dataclass(frozen=True)
class ProviderOperationReservation:
    operation_id: uuid.UUID
    status: str
    provider_object_id: str | None
    attempt_count: int
    replayed: bool


@dataclass(frozen=True)
class ProviderOperationClaim:
    operation_id: uuid.UUID
    status: str
    lease_fence: int
    provider_object_id: str | None
    attempt_count: int
    claimed: bool


@dataclass(frozen=True)
class ProviderOperationResult:
    operation_id: uuid.UUID
    status: str
    provider_object_id: str | None
    attempt_count: int


def provider_checkout_request_hash(
    *,
    payment_id: uuid.UUID,
    provider_code: str,
    environment: str,
    request: ProviderCheckoutIntentRequest,
) -> str:
    canonical = json.dumps(
        {
            "amount": format(request.amount.quantize(Decimal("0.01")), "f"),
            "currency_code": request.currency_code.upper(),
            "environment": environment,
            "idempotency_key": request.idempotency_key,
            "invoice_id": str(request.invoice_id),
            "operation_type": "create_checkout",
            "payment_id": str(payment_id),
            "provider_code": provider_code,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provider_checkout_success_hash(
    response: ProviderCheckoutIntentResponse,
) -> str:
    canonical = json.dumps(
        {
            "provider_code": response.provider_code,
            "provider_order_ref": response.provider_order_ref,
            "status": response.status,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_provider_error_code(error: FinanceProviderOperationError) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", error.code.lower()).strip("_")
    if not normalized:
        return "provider_error"
    return normalized[:80]


class FinanceProviderOperationService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def reserve_checkout(
        self,
        *,
        payment_id: uuid.UUID,
        provider_code: str,
        environment: str,
        idempotency_key: str,
        request_hash_sha256: str,
    ) -> ProviderOperationReservation:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.reserve_finance_provider_operation(
                    :payment_id,
                    :provider_code,
                    :environment,
                    'create_checkout',
                    :idempotency_key,
                    :request_hash
                )
                """
            ),
            {
                "payment_id": payment_id,
                "provider_code": provider_code,
                "environment": environment,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash_sha256,
            },
        )
        return ProviderOperationReservation(**dict(result.mappings().one()))

    async def claim(
        self,
        *,
        operation_id: uuid.UUID,
        lease_owner: uuid.UUID,
    ) -> ProviderOperationClaim:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.claim_finance_provider_operation(
                    :operation_id,
                    :lease_owner
                )
                """
            ),
            {
                "operation_id": operation_id,
                "lease_owner": lease_owner,
            },
        )
        return ProviderOperationClaim(**dict(result.mappings().one()))

    async def finish(
        self,
        *,
        operation_id: uuid.UUID,
        lease_owner: uuid.UUID,
        lease_fence: int,
        outcome: ProviderOperationOutcome,
        provider_object_id: str | None,
        error_code: str | None,
        evidence_sha256: str | None,
    ) -> ProviderOperationResult:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.finish_finance_provider_operation(
                    :operation_id,
                    :lease_owner,
                    :lease_fence,
                    :outcome,
                    :provider_object_id,
                    :error_code,
                    :evidence_sha256
                )
                """
            ),
            {
                "operation_id": operation_id,
                "lease_owner": lease_owner,
                "lease_fence": lease_fence,
                "outcome": outcome,
                "provider_object_id": provider_object_id,
                "error_code": error_code,
                "evidence_sha256": evidence_sha256,
            },
        )
        return ProviderOperationResult(**dict(result.mappings().one()))

    async def reconcile_unknown(
        self,
        *,
        operation_id: uuid.UUID,
        outcome: Literal["succeeded", "failed_final"],
        provider_object_id: str | None,
        error_code: str | None,
        evidence_sha256: str,
    ) -> tuple[uuid.UUID, str, str | None]:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.reconcile_finance_provider_operation(
                    :operation_id,
                    :outcome,
                    :provider_object_id,
                    :error_code,
                    :evidence_sha256
                )
                """
            ),
            {
                "operation_id": operation_id,
                "outcome": outcome,
                "provider_object_id": provider_object_id,
                "error_code": error_code,
                "evidence_sha256": evidence_sha256,
            },
        )
        row = result.mappings().one()
        return row["operation_id"], row["status"], row["provider_object_id"]
