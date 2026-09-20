from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.finance_core.domain.provider_boundary import (
    FinanceProviderOperationError,
    ProviderRefundResponse,
)
from app.finance_core.services.refund_provider_execution import (
    RefundProviderExecutionClaim,
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
