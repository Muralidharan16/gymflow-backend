from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderRefundResponse,
)
from app.finance_core.services.refund_provider_execution import (
    FinanceRefundProviderExecutionService,
    RefundProviderExecutionClaim,
    RefundProviderRequestBinding,
    refund_provider_evidence_hash,
    refund_provider_request_hash,
    safe_refund_provider_error_code,
)


def _claim() -> RefundProviderExecutionClaim:
    return RefundProviderExecutionClaim(
        command_id=uuid.UUID("c2000000-0000-4000-8000-000000000001"),
        refund_id=uuid.UUID("c2000000-0000-4000-8000-000000000002"),
        payment_id=uuid.UUID("c2000000-0000-4000-8000-000000000003"),
        organization_id=uuid.UUID("c2000000-0000-4000-8000-000000000004"),
        provider_code="razorpay_sandbox",
        provider_payment_ref="pay_PAY10CService01",
        amount=Decimal("25.00"),
        currency_code="INR",
        attempt_count=1,
        lease_fence=7,
        reclaimed_existing_attempt=False,
        lease_expires_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )


def test_pay10c_claim_builds_provider_request_only_from_finance_authority():
    claim = _claim()
    request = claim.provider_request()

    assert request.command_id == claim.command_id
    assert request.refund_id == claim.refund_id
    assert request.payment_id == claim.payment_id
    assert request.provider_payment_ref == claim.provider_payment_ref
    assert request.amount == Decimal("25.00")
    assert request.currency_code == "INR"


def test_pay10c_request_hash_is_deterministic_and_environment_bound():
    claim = _claim()

    first = refund_provider_request_hash(claim=claim, environment="test")
    second = refund_provider_request_hash(claim=claim, environment="test")
    sandbox = refund_provider_request_hash(claim=claim, environment="sandbox")

    assert first == second
    assert first != sandbox
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_pay10c_request_hash_changes_for_finance_authority_change():
    claim = _claim()
    changed = RefundProviderExecutionClaim(
        **{
            **claim.__dict__,
            "amount": Decimal("24.99"),
        }
    )

    assert refund_provider_request_hash(
        claim=claim,
        environment="test",
    ) != refund_provider_request_hash(
        claim=changed,
        environment="test",
    )


def _binding(claim: RefundProviderExecutionClaim) -> RefundProviderRequestBinding:
    return RefundProviderRequestBinding(
        command_id=claim.command_id,
        refund_id=claim.refund_id,
        payment_id=claim.payment_id,
        organization_id=claim.organization_id,
        provider_code=claim.provider_code,
        provider_payment_ref=str(claim.provider_payment_ref),
        amount=claim.amount,
        currency_code=claim.currency_code,
        request_sha256="a" * 64,
        status="processing",
    )


def test_pay24b_refund_admission_identity_is_stable_for_reclaim_and_new_for_retry():
    claim = _claim()
    binding = _binding(claim)

    first = FinanceRefundProviderExecutionService.provider_admission_binding(
        claim=claim,
        binding=binding,
    )
    reclaimed = FinanceRefundProviderExecutionService.provider_admission_binding(
        claim=replace(
            claim,
            lease_fence=claim.lease_fence + 1,
            reclaimed_existing_attempt=True,
        ),
        binding=binding,
    )
    retry = FinanceRefundProviderExecutionService.provider_admission_binding(
        claim=replace(
            claim,
            attempt_count=claim.attempt_count + 1,
            lease_fence=claim.lease_fence + 2,
        ),
        binding=binding,
    )

    assert first.logical_operation_id == f"refund:{claim.command_id}:1"
    assert reclaimed == first
    assert retry.logical_operation_id == f"refund:{claim.command_id}:2"
    assert retry.operation_sha == first.operation_sha == binding.request_sha256


def test_pay24b_refund_admission_rejects_pay10_authority_drift():
    claim = _claim()

    with pytest.raises(FinanceProviderConfigError):
        FinanceRefundProviderExecutionService.provider_admission_binding(
            claim=claim,
            binding=replace(_binding(claim), amount=Decimal("24.99")),
        )


class _NoSQLSession:
    async def execute(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("malformed provider success must fail before SQL")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_code", "other_provider"),
        ("provider_code", None),
        ("provider_refund_ref", None),
        ("provider_refund_ref", 123),
        ("provider_refund_ref", ""),
        ("provider_refund_ref", "rfnd_bad/path"),
        ("provider_payment_ref", "pay_wrong"),
        ("provider_payment_ref", None),
        ("amount", Decimal("24.99")),
        ("amount", "25.00"),
        ("currency_code", "USD"),
        ("currency_code", None),
        ("receipt", ""),
        ("receipt", None),
        ("status", "mystery"),
        ("status", None),
        ("status", ["processed"]),
    ],
)
def test_pay24b_malformed_provider_success_is_unknown_before_sql(
    field: str,
    value: object,
):
    claim = _claim()
    response = ProviderRefundResponse(
        provider_code=claim.provider_code,
        provider_refund_ref="rfnd_PAY24BService01",
        provider_payment_ref=str(claim.provider_payment_ref),
        amount=claim.amount,
        currency_code=claim.currency_code,
        receipt="rf_pay24b_service",
        status="processed",
    )
    service = FinanceRefundProviderExecutionService(_NoSQLSession())  # type: ignore[arg-type]

    with pytest.raises(FinanceProviderOperationError) as exc:
        asyncio.run(
            service.record_outcome(
                claim=claim,
                worker_id=uuid.uuid4(),
                response=replace(response, **{field: value}),
            )
        )

    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True


def test_pay10c_normalized_evidence_hash_is_deterministic_and_source_bound():
    response = ProviderRefundResponse(
        provider_code="razorpay_sandbox",
        provider_refund_ref="rfnd_PAY10CService01",
        provider_payment_ref="pay_PAY10CService01",
        amount=Decimal("25.00"),
        currency_code="INR",
        receipt="rf_test_receipt",
        status="processed",
    )
    occurred_at = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    first = refund_provider_evidence_hash(
        response,
        source="reconciliation",
        occurred_at=occurred_at,
    )
    second = refund_provider_evidence_hash(
        response,
        source="reconciliation",
        occurred_at=occurred_at,
    )
    webhook = refund_provider_evidence_hash(
        response,
        source="webhook",
        provider_event_id="evt_pay10c_service",
        occurred_at=occurred_at,
    )

    assert first == second
    assert first != webhook
    assert len(first) == 64


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("RAZORPAY_TIMEOUT", "razorpay_timeout"),
        ("provider error!!!", "provider_error"),
        ("---", "provider_error"),
        ("A" * 100, "a" * 64),
    ],
)
def test_pay10c_error_codes_are_bounded_machine_tokens(
    code: str,
    expected: str,
):
    error = FinanceProviderOperationError(
        provider_code="razorpay_sandbox",
        operation="submit_refund",
        code=code,
        failure_class="unknown",
        message="safe",
    )

    actual = safe_refund_provider_error_code(error)

    assert actual == expected
    assert len(actual) <= 64
    assert actual.replace("_", "").isalnum()
