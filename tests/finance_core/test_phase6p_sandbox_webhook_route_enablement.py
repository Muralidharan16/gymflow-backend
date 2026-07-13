from __future__ import annotations

import hmac
import uuid

import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.middleware import _is_exempt
from app.finance_core.api.guards import FinanceWebhookRoutePosture, get_finance_webhook_route_posture
from app.finance_core.api.payment_boundary import get_razorpay_webhook_confirmation_service
from app.finance_core.services.razorpay_webhooks import RazorpayWebhookConfirmationService
from app.main import app
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import PROVIDER_CONFIG, razorpay_payload, seed_checkout
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts
from tests.finance_core.test_phase6i_payment_api_auth_rbac_tenant_isolation import auth_headers


class ForbiddenWebhookConfirmationService:
    def __init__(self):
        self.called = False

    async def confirm_payment_event(self, webhook):
        self.called = True
        raise AssertionError("disabled webhook route must not call confirmation service")


def sandbox_webhook_posture(**overrides) -> FinanceWebhookRoutePosture:
    values = {
        "sandbox_webhook_enabled": True,
        "provider_mode": "sandbox",
    }
    values.update(overrides)
    return FinanceWebhookRoutePosture(**values)


def signed_headers(raw_body: bytes, *, idempotency_key: str = "phase6p-webhook", signature: str | None = None) -> dict[str, str]:
    return {
        "X-Razorpay-Signature": signature if signature is not None else hmac.digest(b"rzp_webhook_secret", raw_body, "sha256").hex(),
        "X-Idempotency-Key": idempotency_key,
    }


def override_webhook_dependencies(posture: FinanceWebhookRoutePosture):
    async def service_override(db: AsyncSession = Depends(get_db)) -> RazorpayWebhookConfirmationService:
        return RazorpayWebhookConfirmationService(
            db,
            razorpay_config=sandbox_config(webhook_secret="rzp_webhook_secret"),
            provider_config=PROVIDER_CONFIG,
        )

    app.dependency_overrides[get_finance_webhook_route_posture] = lambda: posture
    app.dependency_overrides[get_razorpay_webhook_confirmation_service] = service_override


def clear_webhook_dependency_overrides() -> None:
    app.dependency_overrides.pop(get_finance_webhook_route_posture, None)
    app.dependency_overrides.pop(get_razorpay_webhook_confirmation_service, None)


@pytest.mark.asyncio
async def test_default_webhook_route_remains_disabled_before_body_processing_or_service_call(client):
    service = ForbiddenWebhookConfirmationService()
    app.dependency_overrides[get_razorpay_webhook_confirmation_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b"not-json-and-not-verified",
            headers={"X-Razorpay-Signature": "syntactic-signature-only", "X-Idempotency-Key": "phase6p-default-disabled"},
        )
    finally:
        app.dependency_overrides.pop(get_razorpay_webhook_confirmation_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_sandbox_webhook_route_accepts_valid_signed_captured_event_and_updates_state_only(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6p_captured", event_type="payment.captured", payment_id="pay_phase6p", status="captured")
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6p-captured"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"
    assert await fetch_scalar("SELECT provider_payment_ref FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "pay_phase6p"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6p_captured'") == 1
    await assert_no_webhook_financial_side_effects()


@pytest.mark.asyncio
async def test_sandbox_webhook_route_rejects_invalid_signature_before_mutation(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6p_bad_signature", payment_id="pay_bad_signature")
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6p-bad-signature", signature="bad"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_SIGNATURE_INVALID"
    assert "secret" not in str(response.json()).lower()
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_sandbox_webhook_route_replay_is_idempotent_and_does_not_duplicate_state_event(client):
    await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6p_replay", event_type="payment.authorized", payment_id="pay_phase6p_replay", status="authorized")
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        first = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6p-replay"),
        )
        replay = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6p-replay"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert first.status_code == 202
    assert replay.status_code == 202
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6p_replay'") == 1


@pytest.mark.asyncio
async def test_sandbox_webhook_route_rejects_unknown_provider_reference_without_mutation(client):
    await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6p_unknown_order", order_id="order_missing", payment_id="pay_missing")
    override_webhook_dependencies(sandbox_webhook_posture())
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6p-unknown-order"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_PAYLOAD_INVALID"
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_webhook_route_rejects_unsafe_sandbox_postures_before_service_or_mutation(client):
    unsafe_postures = [
        sandbox_webhook_posture(provider_mode="live"),
        sandbox_webhook_posture(live_provider_enabled=True),
        sandbox_webhook_posture(live_money_movement_enabled=True),
        sandbox_webhook_posture(production_webhook_enabled=True),
        sandbox_webhook_posture(internal_payment_application_enabled=True),
    ]

    for index, posture in enumerate(unsafe_postures):
        await seed_checkout()
        raw = razorpay_payload(event_id=f"evt_phase6p_unsafe_{index}", payment_id=f"pay_unsafe_{index}")
        override_webhook_dependencies(posture)
        before = await finance_counts()
        try:
            response = await client.post(
                "/api/v1/finance/payments/webhooks/razorpay",
                content=raw,
                headers=signed_headers(raw, idempotency_key=f"phase6p-unsafe-{index}"),
            )
        finally:
            clear_webhook_dependency_overrides()

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_SANDBOX_POSTURE_UNSAFE"
        assert await finance_counts() == before


@pytest.mark.asyncio
async def test_webhook_sandbox_enablement_does_not_enable_checkout_internal_apply_status_or_admin_routes(client):
    await seed_checkout()
    override_webhook_dependencies(sandbox_webhook_posture())
    before = await finance_counts()
    try:
        checkout_response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(role="admin"), "X-Idempotency-Key": "phase6p-checkout-still-disabled"},
            json={"plan_code": "DOERS_PRO_MONTHLY", "billing_interval": "monthly", "billing_party_id": str(uuid.uuid4())},
        )
        status_response = await client.get(
            f"/api/v1/finance/payments/checkout-sessions/{uuid.uuid4()}",
            headers=auth_headers(role="admin"),
        )
        apply_response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers={**auth_headers(role="admin"), "X-Finance-Internal-Actor": "finance_core"},
            json={
                "payment_id": str(uuid.uuid4()),
                "invoice_id": str(uuid.uuid4()),
                "amount": "1180.00",
                "currency_code": "INR",
                "idempotency_key": "phase6p-apply-disabled",
                "internal_actor": "finance_core",
                "reason": "webhook sandbox must not enable apply",
            },
        )
        admin_response = await client.get(
            f"/api/v1/finance/payments/admin/payments/{uuid.uuid4()}",
            headers=auth_headers(role="finance_admin"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert_disabled(checkout_response)
    assert_disabled(status_response)
    assert_disabled(apply_response)
    assert_disabled(admin_response)
    assert await finance_counts() == before


def test_webhook_jwt_exemption_is_exact_path_only():
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay") is True
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay/extra") is False
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay-live") is False
    assert _is_exempt("/api/v1/finance/payments/checkout-sessions") is False
    assert _is_exempt("/static/logo.png") is True


async def assert_no_webhook_financial_side_effects():
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.reconciled'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.refunds") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.credit_notes") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0
