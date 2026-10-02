from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
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
class RefundProviderAdmissionBinding:
    logical_operation_id: str
    operation_sha: str


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

    This service does no provider network I/O and does not commit. The
    caller must establish short transaction boundaries around claim/bind and
    outcome recording, keeping provider calls outside database transactions.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    @staticmethod
    def provider_admission_binding(
        *,
        claim: RefundProviderExecutionClaim,
        binding: RefundProviderRequestBinding,
    ) -> RefundProviderAdmissionBinding:
        """Bind PAY-24 admission to one durable PAY-10 provider attempt.

        PAY-10 preserves ``attempt_count`` when reclaiming an expired
        processing lease, but increments it for a new explicitly permitted
        retry after known non-acceptance.  It is therefore the durable attempt
        identity: using ``lease_fence`` here would create a fresh admission on
        crash recovery and could permit a blind provider resubmission.
        """

        if (
            binding.command_id != claim.command_id
            or binding.refund_id != claim.refund_id
            or binding.payment_id != claim.payment_id
            or binding.organization_id != claim.organization_id
            or binding.provider_code != claim.provider_code
            or binding.provider_payment_ref != claim.provider_payment_ref
            or binding.amount != claim.amount
            or binding.currency_code.upper() != claim.currency_code.upper()
            or binding.status != "processing"
            or not re.fullmatch(r"[0-9a-f]{64}", binding.request_sha256)
            or claim.attempt_count < 1
        ):
            raise FinanceProviderConfigError(
                "PAY-10 refund admission binding differs from durable authority."
            )

        return RefundProviderAdmissionBinding(
            logical_operation_id=(
                f"refund:{claim.command_id}:{claim.attempt_count}"
            ),
            operation_sha=binding.request_sha256,
        )

    @staticmethod
    def validate_provider_response(
        *,
        claim: RefundProviderExecutionClaim,
        response: object,
    ) -> ProviderRefundResponse:
        """Validate untrusted post-I/O data before normalization or hashing.

        ``ProviderRefundResponse`` is a Python dataclass rather than a runtime
        schema.  An adapter can therefore construct it with values that do not
        match its annotations.  Keep this check total over those malformed
        values so every untrustworthy post-I/O result follows the PAY-10/PAY-24
        unknown path instead of escaping as ``AttributeError``/``TypeError``.
        """

        request = claim.provider_request()
        if type(response) is not ProviderRefundResponse:
            raise _authority_mismatch(claim)

        if (
            type(response.provider_code) is not str
            or not response.provider_code
            or type(response.provider_refund_ref) is not str
            or type(response.provider_payment_ref) is not str
            or not response.provider_payment_ref
            or type(response.amount) is not Decimal
            or type(response.currency_code) is not str
            or type(response.receipt) is not str
            or not (1 <= len(response.receipt) <= 200)
            or type(response.status) is not str
        ):
            raise _authority_mismatch(claim)

        if (
            re.fullmatch(
                r"[A-Za-z0-9_-]{1,200}",
                response.provider_refund_ref,
            )
            is None
            or response.status not in {"pending", "processed", "failed"}
            or response.provider_code != claim.provider_code
            or response.provider_payment_ref != request.provider_payment_ref
            or not response.amount.is_finite()
        ):
            raise _authority_mismatch(claim)

        # Prove the exact operation used by evidence hashing is safe before
        # the response can reach that function. DecimalException covers values
        # whose shape exceeds the active decimal context even when finite.
        try:
            quantized_amount = response.amount.quantize(Decimal("0.01"))
        except DecimalException as exc:
            raise _authority_mismatch(claim) from exc

        if (
            quantized_amount != response.amount
            or response.amount != request.amount
            or not re.fullmatch(r"[A-Za-z]{3}", response.currency_code)
            or response.currency_code.upper()
            != request.currency_code.upper()
        ):
            raise _authority_mismatch(claim)

        return response

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
        occurred_at: datetime | None = None,
    ) -> RefundProviderEvidenceReceipt:
        response = self.validate_provider_response(
            claim=claim,
            response=response,
        )

        # Validate the provider response before hashing or executing SQL.  Any
        # malformed success is a post-I/O ambiguity and must be handled by the
        # worker's atomic PAY-10/PAY-24 unknown terminal transaction.
        evidence_sha256 = refund_provider_evidence_hash(
            response,
            source="submission",
            occurred_at=occurred_at,
        )

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
