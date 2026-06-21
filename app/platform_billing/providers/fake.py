from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.platform_billing.domain.hashing import CanonicalSerializer
from app.platform_billing.domain.provider_operations import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderOutcomeKind,
)


Probe = Callable[[ProviderCallRequest], bool | Awaitable[bool]]


@dataclass
class FakeProviderCallRecord:
    request: ProviderCallRequest
    active_transaction_observed: bool


@dataclass
class DeterministicFakeProvider:
    outcome: ProviderOutcomeKind = ProviderOutcomeKind.SUCCESS
    transaction_probe: Probe | None = None
    calls: list[FakeProviderCallRecord] = field(default_factory=list)

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        active_transaction = False
        if self.transaction_probe is not None:
            probe_result = self.transaction_probe(request)
            if hasattr(probe_result, "__await__"):
                probe_result = await probe_result  # type: ignore[assignment]
            active_transaction = bool(probe_result)

        self.calls.append(
            FakeProviderCallRecord(
                request=request,
                active_transaction_observed=active_transaction,
            )
        )
        reference = stable_fake_reference(request)
        if self.outcome is ProviderOutcomeKind.SUCCESS:
            return ProviderCallResult(
                outcome=self.outcome,
                external_operation_ref=reference,
                result_reference=f"fake-result-{reference}",
            )
        if self.outcome is ProviderOutcomeKind.BUSINESS_FAILURE:
            return ProviderCallResult(
                outcome=self.outcome,
                external_operation_ref=reference,
                error_classification="provider_declined",
                result_reference=f"fake-decline-{reference}",
            )
        if self.outcome is ProviderOutcomeKind.RETRYABLE_FAILURE:
            return ProviderCallResult(
                outcome=self.outcome,
                external_operation_ref=None,
                error_classification="provider_retryable_failure",
                result_reference=f"fake-retryable-{reference}",
            )
        if self.outcome is ProviderOutcomeKind.TIMEOUT:
            return ProviderCallResult(
                outcome=self.outcome,
                external_operation_ref=None,
                error_classification="provider_timeout",
                result_reference=f"fake-timeout-{reference}",
            )
        return ProviderCallResult(
            outcome=ProviderOutcomeKind.UNKNOWN,
            external_operation_ref=reference,
            error_classification="provider_outcome_unknown",
            result_reference=f"fake-unknown-{reference}",
        )


def stable_fake_reference(request: ProviderCallRequest) -> str:
    canonical = CanonicalSerializer.serialize(
        {
            "operation_id": request.operation_id,
            "organization_id": request.organization_id,
            "operation_type": request.operation_type,
            "amount_minor": request.amount_minor,
            "currency_code": request.currency_code,
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"fake_op_{digest}"
