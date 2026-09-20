from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderEnvironment,
    ProviderRefundRequest,
    ProviderRefundResponse,
)


RefundEvidenceSource = Literal["submission", "webhook", "reconciliation"]
RefundNormalizedStatus = Literal["pending", "processed", "failed"]


@dataclass(frozen=True)
class RefundProviderExecutionClaim:
    command_id: uuid.UUID
    refund_id: uuid.UUID
    payment_id: uuid.UUID
    organization_id: uuid.UUID
    provider_code: str
    provider_payment_ref: str | None
    amount: Decimal
    currency_code: str
    attempt_count: int
    lease_fence: int
    reclaimed_existing_attempt: bool
    lease_expires_at: datetime

    def provider_request(self) -> ProviderRefundRequest:
        if not self.provider_payment_ref:
            raise FinanceProviderConfigError(
                "Refund provider payment reference is missing from Finance truth."
            )
        return ProviderRefundRequest(
            command_id=self.command_id,
            refund_id=self.refund_id,
            payment_id=self.payment_id,
            provider_payment_ref=self.provider_payment_ref,
            amount=self.amount,
            currency_code=self.currency_code,
        )


@dataclass(frozen=True)
class RefundProviderRequestBinding:
    command_id: uuid.UUID
    refund_id: uuid.UUID
    payment_id: uuid.UUID
    organization_id: uuid.UUID
    provider_code: str
    provider_payment_ref: str
    amount: Decimal
    currency_code: str
    request_sha256: str
    status: str


@dataclass(frozen=True)
class RefundProviderEvidenceReceipt:
    evidence_id: uuid.UUID
    command_id: uuid.UUID
    normalized_status: str
    command_status: str
    replayed: bool


def refund_provider_request_hash(
    *,
    claim: RefundProviderExecutionClaim,
    environment: ProviderEnvironment,
) -> str:
    request = claim.provider_request()
    canonical = json.dumps(
        {
            "amount": format(
                request.amount.quantize(Decimal("0.01")),
                "f",
            ),
            "command_id": str(request.command_id),
            "currency_code": request.currency_code.upper(),
            "environment": environment,
            "operation_type": "refund",
            "payment_id": str(request.payment_id),
            "provider_code": claim.provider_code,
            "provider_payment_ref": request.provider_payment_ref,
            "refund_id": str(request.refund_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def refund_provider_evidence_hash(
    response: ProviderRefundResponse,
    *,
    source: RefundEvidenceSource,
    provider_event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> str:
    canonical = json.dumps(
        {
            "amount": format(response.amount.quantize(Decimal("0.01")), "f"),
            "currency_code": response.currency_code.upper(),
            "evidence_source": source,
            "normalized_status": response.status,
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "provider_code": response.provider_code,
            "provider_event_id": provider_event_id,
            "provider_payment_ref": response.provider_payment_ref,
            "provider_refund_ref": response.provider_refund_ref,
            "receipt": response.receipt,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_refund_provider_error_code(
    error: FinanceProviderOperationError,
) -> str:
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        error.code.lower(),
    ).strip("_")
    if not normalized:
        return "provider_error"
    return normalized[:64]


def _authority_mismatch(
    claim: RefundProviderExecutionClaim,
) -> FinanceProviderOperationError:
    return FinanceProviderOperationError(
        provider_code=claim.provider_code,
        operation="refund_outcome",
        code="PROVIDER_REFUND_AUTHORITY_MISMATCH",
        failure_class="unknown",
        message="Provider refund response did not match Finance authority.",
    )


class FinanceRefundProviderExecutionService:
    """Bounded PAY-10-C database capability wrapper.

    This service performs no provider network I/O and does not commit. The
    caller must establish short transaction boundaries around claim/bind and
    outcome recording, keeping provider calls outside database transactions.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def claim(
        self,
        *,
        worker_id: uuid.UUID,
        limit: int = 1,
    ) -> list[RefundProviderExecutionClaim]:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.claim_pay10_refund_provider_execution(
                    :worker_id,
                    :limit
                )
                """
            ),
            {"worker_id": worker_id, "limit": limit},
        )
        return [
            RefundProviderExecutionClaim(**dict(row))
            for row in result.mappings().all()
        ]

    async def bind_request(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        environment: ProviderEnvironment,
    ) -> RefundProviderRequestBinding:
        request = claim.provider_request()
        request_sha256 = refund_provider_request_hash(
            claim=claim,
            environment=environment,
        )
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.bind_pay10_refund_provider_request(
                    :command_id,
                    :worker_id,
                    :lease_fence,
                    :provider_code,
                    :provider_payment_ref,
                    :amount,
                    :currency_code,
                    :request_sha256
                )
                """
            ),
            {
                "command_id": claim.command_id,
                "worker_id": claim_worker_id(claim),
                "lease_fence": claim.lease_fence,
                "provider_code": claim.provider_code,
                "provider_payment_ref": request.provider_payment_ref,
                "amount": request.amount,
                "currency_code": request.currency_code,
                "request_sha256": request_sha256,
            },
        )
        return RefundProviderRequestBinding(**dict(result.mappings().one()))

    async def bind_request_for_worker(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        environment: ProviderEnvironment,
    ) -> RefundProviderRequestBinding:
        request = claim.provider_request()
        request_sha256 = refund_provider_request_hash(
            claim=claim,
            environment=environment,
        )
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.bind_pay10_refund_provider_request(
                    :command_id,
                    :worker_id,
                    :lease_fence,
                    :provider_code,
                    :provider_payment_ref,
                    :amount,
                    :currency_code,
                    :request_sha256
                )
                """
            ),
            {
                "command_id": claim.command_id,
                "worker_id": worker_id,
                "lease_fence": claim.lease_fence,
                "provider_code": claim.provider_code,
                "provider_payment_ref": request.provider_payment_ref,
                "amount": request.amount,
                "currency_code": request.currency_code,
                "request_sha256": request_sha256,
            },
        )
        return RefundProviderRequestBinding(**dict(result.mappings().one()))

    async def record_outcome(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        response: ProviderRefundResponse,
        evidence_sha256: str,
        occurred_at: datetime | None = None,
    ) -> RefundProviderEvidenceReceipt:
        request = claim.provider_request()
        if (
            response.provider_code != claim.provider_code
            or response.provider_payment_ref != request.provider_payment_ref
            or response.amount != request.amount
            or response.currency_code.upper() != request.currency_code.upper()
        ):
            raise _authority_mismatch(claim)

        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.record_pay10_refund_provider_outcome(
                    :command_id,
                    :worker_id,
                    :lease_fence,
                    :provider_refund_ref,
                    :normalized_status,
                    :evidence_sha256,
                    :occurred_at
                )
                """
            ),
            {
                "command_id": claim.command_id,
                "worker_id": worker_id,
                "lease_fence": claim.lease_fence,
                "provider_refund_ref": response.provider_refund_ref,
                "normalized_status": response.status,
                "evidence_sha256": evidence_sha256,
                "occurred_at": occurred_at,
            },
        )
        return RefundProviderEvidenceReceipt(**dict(result.mappings().one()))

    async def record_unknown(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        error: FinanceProviderOperationError,
    ) -> str:
        result = await self._session.execute(
            text(
                """
                SELECT app_secure.record_pay10_refund_provider_unknown(
                    :command_id,
                    :worker_id,
                    :lease_fence,
                    :error_code
                )
                """
            ),
            {
                "command_id": claim.command_id,
                "worker_id": worker_id,
                "lease_fence": claim.lease_fence,
                "error_code": safe_refund_provider_error_code(error),
            },
        )
        return str(result.scalar_one())

    async def record_failure(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        error: FinanceProviderOperationError,
        permanent: bool,
    ) -> str:
        result = await self._session.execute(
            text(
                """
                SELECT app_secure.record_pay10_refund_provider_failure(
                    :command_id,
                    :worker_id,
                    :lease_fence,
                    :error_code,
                    :permanent
                )
                """
            ),
            {
                "command_id": claim.command_id,
                "worker_id": worker_id,
                "lease_fence": claim.lease_fence,
                "error_code": safe_refund_provider_error_code(error),
                "permanent": permanent,
            },
        )
        return str(result.scalar_one())

    async def record_external_evidence(
        self,
        *,
        command_id: uuid.UUID,
        provider_payment_ref: str,
        provider_event_id: str | None,
        provider_refund_ref: str | None,
        source: Literal["webhook", "reconciliation"],
        normalized_status: RefundNormalizedStatus,
        evidence_sha256: str,
        occurred_at: datetime | None = None,
    ) -> RefundProviderEvidenceReceipt:
        result = await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.record_pay10_refund_external_evidence(
                    :command_id,
                    :provider_payment_ref,
                    :provider_event_id,
                    :provider_refund_ref,
                    :evidence_source,
                    :normalized_status,
                    :evidence_sha256,
                    :occurred_at
                )
                """
            ),
            {
                "command_id": command_id,
                "provider_payment_ref": provider_payment_ref,
                "provider_event_id": provider_event_id,
                "provider_refund_ref": provider_refund_ref,
                "evidence_source": source,
                "normalized_status": normalized_status,
                "evidence_sha256": evidence_sha256,
                "occurred_at": occurred_at,
            },
        )
        return RefundProviderEvidenceReceipt(**dict(result.mappings().one()))


def claim_worker_id(
    claim: RefundProviderExecutionClaim,
) -> uuid.UUID:
    raise FinanceProviderConfigError(
        "bind_request requires an explicit worker id; use bind_request_for_worker."
    )
