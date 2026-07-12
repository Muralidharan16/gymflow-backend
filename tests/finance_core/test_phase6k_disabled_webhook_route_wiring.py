from __future__ import annotations

import uuid

import pytest

from app.finance_core.api.payment_boundary import (
    build_razorpay_webhook_input,
    get_razorpay_webhook_confirmation_service,
)
from app.finance_core.api.auth import FinancePaymentActorKind, require_webhook_signature_actor
from app.main import app
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts


class ForbiddenRazorpayWebhookConfirmationService:
    def __init__(self):
        self.called = False

    async def confirm_payment_event(self, webhook):
        self.called = True
        raise AssertionError("disabled webhook route must not call confirmation service")


@pytest.mark.asyncio
async def test_disabled_webhook_route_rejects_before_confirmation_signature_or_body_processing(client):
    service = ForbiddenRazorpayWebhookConfirmationService()
    app.dependency_overrides[get_razorpay_webhook_confirmation_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b"not-json-and-not-verified",
            headers={
                "X-Razorpay-Signature": "syntactic-signature-only",
                "X-Idempotency-Key": "phase6k-disabled-webhook",
            },
        )
    finally:
        app.dependency_overrides.pop(get_razorpay_webhook_confirmation_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_webhook_route_does_not_require_user_session_or_real_secret(client):
    service = ForbiddenRazorpayWebhookConfirmationService()
    app.dependency_overrides[get_razorpay_webhook_confirmation_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b'{"event":"payment.captured"}',
            headers={"X-Razorpay-Signature": "not-a-real-secret-derived-signature"},
        )
    finally:
        app.dependency_overrides.pop(get_razorpay_webhook_confirmation_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_webhook_route_does_not_mutate_payment_state_allocation_ledger_or_paid_invoice(client):
    service = ForbiddenRazorpayWebhookConfirmationService()
    app.dependency_overrides[get_razorpay_webhook_confirmation_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b'{"event":"order.paid"}',
            headers={"X-Razorpay-Signature": "syntactic-signature-only"},
        )
    finally:
        app.dependency_overrides.pop(get_razorpay_webhook_confirmation_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


def test_webhook_input_mapping_preserves_raw_body_signature_and_idempotency_key():
    raw_body = b'{"event":"payment.captured","payload":{}}'

    webhook = build_razorpay_webhook_input(
        raw_body=raw_body,
        signature="signature-fixture",
        idempotency_key="webhook-idempotency-key",
    )

    assert webhook.raw_body == raw_body
    assert webhook.signature == "signature-fixture"
    assert webhook.idempotency_key == "webhook-idempotency-key"


def test_webhook_actor_contract_is_signature_context_only_and_not_tenant_actor():
    webhook_actor = require_webhook_signature_actor("signature-fixture")

    assert webhook_actor.kind == FinancePaymentActorKind.WEBHOOK
    assert webhook_actor.organization_id is None
    assert webhook_actor.staff_id is None
    assert webhook_actor.kind not in {
        FinancePaymentActorKind.TENANT_ADMIN,
        FinancePaymentActorKind.CUSTOMER,
        FinancePaymentActorKind.INTERNAL_SYSTEM,
    }


def test_phase6k_source_has_no_live_provider_frontend_subscription_or_production_enablement():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "razorpay.client" not in combined
    assert "razorpayclient" not in combined
    assert "provider_secret" not in combined
    assert "rzp_live_" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "entitlement_projection" not in combined
    assert not (repo_root / "frontend").exists()
