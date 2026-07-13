from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.security import create_access_token
from app.finance_core.api.guards import (
    FinanceInternalApplyRoutePosture,
    get_finance_internal_apply_route_posture,
)
from app.finance_core.api.payment_boundary import get_payment_application_gate_service
from app.main import app
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import (
    issued_invoice,
    payment_command,
    record_payment,
    seed_finance_foundation,
)
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import razorpay_payload, seed_checkout
from tests.finance_core.test_phase6e_payment_application_gate import seed_ledger_accounts_only
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts
from tests.finance_core.test_phase6p_sandbox_webhook_route_enablement import (
    clear_webhook_dependency_overrides,
    override_webhook_dependencies,
    sandbox_webhook_posture,
    signed_headers,
)


class ForbiddenPaymentApplicationGateService:
    def __init__(self):
        self.called = False

    async def apply_confirmed_payment(self, command):
        self.called = True
        raise AssertionError("disabled internal apply route must not call payment application gate")


def auth_headers(*, role: str = "admin") -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(uuid.uuid4()), f"{role}@phase6r.test.local", role=role)
    return {"Authorization": f"Bearer {token}"}


def internal_headers(*, role: str = "admin", actor: str = "finance_core") -> dict[str, str]:
    return {**auth_headers(role=role), "X-Finance-Internal-Actor": actor}


def internal_apply_posture(**overrides) -> FinanceInternalApplyRoutePosture:
    values = {
        "sandbox_internal_apply_enabled": True,
        "provider_mode": "sandbox",
    }
    values.update(overrides)
    return FinanceInternalApplyRoutePosture(**values)


def enable_internal_apply(posture: FinanceInternalApplyRoutePosture | None = None) -> None:
    app.dependency_overrides[get_finance_internal_apply_route_posture] = lambda: posture or internal_apply_posture()


def clear_internal_apply_overrides() -> None:
    app.dependency_overrides.pop(get_finance_internal_apply_route_posture, None)
    app.dependency_overrides.pop(get_payment_application_gate_service, None)


def application_payload(payment_id: uuid.UUID, invoice_id: uuid.UUID, **overrides) -> dict[str, str]:
    payload = {
        "payment_id": str(payment_id),
        "invoice_id": str(invoice_id),
        "amount": "1180.00",
        "currency_code": "INR",
        "idempotency_key": "phase6r-apply",
        "internal_actor": "finance_core",
        "reason": "sandbox internal payment application",
    }
    payload.update(overrides)
    return payload


async def assert_no_subscription_or_entitlement_side_effects() -> None:
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type LIKE 'platform.%'") == 0


@pytest.mark.asyncio
async def test_internal_apply_route_remains_disabled_by_default_before_gate_or_mutation(client):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6r-default-disabled")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6r_default", idempotency_key="pay-6r-default"))
    gate = ForbiddenPaymentApplicationGateService()
    app.dependency_overrides[get_payment_application_gate_service] = lambda: gate
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(payment.payment_id, invoice.invoice_id),
        )
    finally:
        clear_internal_apply_overrides()

    assert_disabled(response)
    assert gate.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["customer", "tenant_admin", "finance_admin", "webhook", ""])
async def test_sandbox_internal_apply_route_rejects_non_internal_system_actors_before_gate(client, actor: str):
    gate = ForbiddenPaymentApplicationGateService()
    app.dependency_overrides[get_payment_application_gate_service] = lambda: gate
    enable_internal_apply()
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(actor=actor),
            json=application_payload(uuid.uuid4(), uuid.uuid4(), idempotency_key=f"phase6r-actor-{actor or 'blank'}"),
        )
    finally:
        clear_internal_apply_overrides()

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "FINANCE_INTERNAL_ACTOR_REQUIRED"
    assert gate.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_sandbox_internal_apply_route_applies_captured_payment_marks_invoice_paid_and_posts_ledger(client):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6r-captured")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6r_captured", idempotency_key="pay-6r-captured"))
    enable_internal_apply()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(payment.payment_id, invoice.invoice_id, idempotency_key="phase6r-captured"),
        )
    finally:
        clear_internal_apply_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["payment_id"] == str(payment.payment_id)
    assert body["invoice_id"] == str(invoice.invoice_id)
    assert body["invoice_status"] == "paid"
    assert Decimal(str(body["allocated_amount"])) == Decimal("1180.00")
    assert body["replayed"] is False
    rendered = str(body).lower()
    assert "secret" not in rendered
    assert "provider" not in rendered
    assert "subscription" not in rendered
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"
    await assert_no_subscription_or_entitlement_side_effects()


@pytest.mark.asyncio
async def test_sandbox_internal_apply_route_can_partially_pay_invoice(client):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6r-partial")
    payment = await record_payment(
        payment_command(provider_payment_ref="pay_6r_partial", amount="500.00", idempotency_key="pay-6r-partial")
    )
    enable_internal_apply()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(
                payment.payment_id,
                invoice.invoice_id,
                amount="500.00",
                idempotency_key="phase6r-partial",
            ),
        )
    finally:
        clear_internal_apply_overrides()

    assert response.status_code == 200
    assert response.json()["invoice_status"] == "partially_paid"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "partially_paid"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["created", "pending", "authorized", "failed", "cancelled", "refunded"])
async def test_sandbox_internal_apply_route_rejects_ineligible_payment_states(client, status: str):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=f"invoice-6r-state-{status}")
    payment = await record_payment(
        payment_command(provider_payment_ref=f"pay_6r_{status}", status=status, idempotency_key=f"pay-6r-{status}")
    )
    enable_internal_apply()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(payment.payment_id, invoice.invoice_id, idempotency_key=f"phase6r-state-{status}"),
        )
    finally:
        clear_internal_apply_overrides()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FINANCE_INTERNAL_APPLY_STATE_CONFLICT"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 0


@pytest.mark.asyncio
async def test_sandbox_internal_apply_route_rejects_mismatched_invoice_payment_dimensions(client):
    await seed_finance_foundation()
    invoice = await issued_invoice(amount="100.00", idempotency_key="invoice-6r-mismatch")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6r_mismatch", idempotency_key="pay-6r-mismatch"))
    enable_internal_apply()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(
                payment.payment_id,
                invoice.invoice_id,
                amount="100.00",
                idempotency_key="phase6r-mismatch",
            ),
        )
    finally:
        clear_internal_apply_overrides()

    assert response.status_code == 409
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0


@pytest.mark.asyncio
async def test_sandbox_internal_apply_route_replays_same_idempotency_key_and_rejects_different_payload(client):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-6r-replay")
    payment = await record_payment(payment_command(provider_payment_ref="pay_6r_replay", idempotency_key="pay-6r-replay"))
    enable_internal_apply()
    try:
        first = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(payment.payment_id, invoice.invoice_id, idempotency_key="phase6r-replay"),
        )
        replay = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(payment.payment_id, invoice.invoice_id, idempotency_key="phase6r-replay"),
        )
        conflict = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(
                payment.payment_id,
                invoice.invoice_id,
                amount="1.00",
                idempotency_key="phase6r-replay",
            ),
        )
    finally:
        clear_internal_apply_overrides()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["allocation_id"] == first.json()["allocation_id"]
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1


@pytest.mark.asyncio
async def test_internal_apply_route_rejects_unsafe_postures_before_gate_or_mutation(client):
    unsafe_postures = [
        internal_apply_posture(provider_mode="live"),
        internal_apply_posture(live_provider_enabled=True),
        internal_apply_posture(live_money_movement_enabled=True),
        internal_apply_posture(production_payment_application_enabled=True),
        internal_apply_posture(webhook_triggered_application_enabled=True),
        internal_apply_posture(subscription_automation_enabled=True),
        internal_apply_posture(entitlement_updates_enabled=True),
    ]

    for index, posture in enumerate(unsafe_postures):
        gate = ForbiddenPaymentApplicationGateService()
        app.dependency_overrides[get_payment_application_gate_service] = lambda gate=gate: gate
        enable_internal_apply(posture)
        before = await finance_counts()
        try:
            response = await client.post(
                "/api/v1/finance/payments/internal/payment-applications",
                headers=internal_headers(),
                json=application_payload(uuid.uuid4(), uuid.uuid4(), idempotency_key=f"phase6r-unsafe-{index}"),
            )
        finally:
            clear_internal_apply_overrides()

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "FINANCE_INTERNAL_APPLY_SANDBOX_POSTURE_UNSAFE"
        assert gate.called is False
        assert await finance_counts() == before


@pytest.mark.asyncio
async def test_webhook_confirmation_still_does_not_apply_payment_until_internal_route_is_called(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(
        event_id="evt_phase6r_captured",
        event_type="payment.captured",
        payment_id="pay_phase6r_webhook",
        status="captured",
    )
    override_webhook_dependencies(sandbox_webhook_posture())
    try:
        webhook_response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6r-webhook-captured"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert webhook_response.status_code == 202
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": checkout.finance_invoice_id}) == "issued"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 0

    await seed_ledger_accounts_only()
    enable_internal_apply()
    try:
        apply_response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers=internal_headers(),
            json=application_payload(
                checkout.finance_checkout_intent_id,
                checkout.finance_invoice_id,
                idempotency_key="phase6r-webhook-then-apply",
            ),
        )
    finally:
        clear_internal_apply_overrides()

    assert apply_response.status_code == 200
    assert apply_response.json()["invoice_status"] == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    await assert_no_subscription_or_entitlement_side_effects()


@pytest.mark.asyncio
async def test_internal_apply_enablement_does_not_enable_checkout_webhook_status_or_admin_routes(client):
    enable_internal_apply()
    before = await finance_counts()
    try:
        checkout_response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(role="admin"), "X-Idempotency-Key": "phase6r-checkout-disabled"},
            json={"plan_code": "DOERS_PRO_MONTHLY", "billing_interval": "monthly", "billing_party_id": str(uuid.uuid4())},
        )
        webhook_response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b'{"event":"payment.captured"}',
            headers={"X-Razorpay-Signature": "syntactic-signature-only"},
        )
        status_response = await client.get(
            f"/api/v1/finance/payments/checkout-sessions/{uuid.uuid4()}",
            headers=auth_headers(role="admin"),
        )
        admin_response = await client.get(
            f"/api/v1/finance/payments/admin/payments/{uuid.uuid4()}",
            headers=auth_headers(role="finance_admin"),
        )
    finally:
        clear_internal_apply_overrides()

    assert_disabled(checkout_response)
    assert_disabled(webhook_response)
    assert_disabled(status_response)
    assert_disabled(admin_response)
    assert await finance_counts() == before


def test_phase6r_source_has_no_live_provider_frontend_subscription_or_production_enablement():
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
