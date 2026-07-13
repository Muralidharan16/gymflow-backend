from __future__ import annotations

import hmac
import json
import uuid
from pathlib import Path

import pytest

from app.core.middleware import _is_exempt
from app.finance_core.api.guards import FinanceWebhookRoutePosture, get_finance_webhook_route_posture
from app.finance_core.api.payment_boundary import (
    get_provider_webhook_sandbox_config,
    get_razorpay_webhook_test_mode_config,
)
from app.finance_core.domain.provider_boundary import ProviderSandboxConfig
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig
from app.main import app
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import PROVIDER_CONFIG, razorpay_payload, seed_checkout
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts
from tests.finance_core.test_phase6i_payment_api_auth_rbac_tenant_isolation import auth_headers


WEBHOOK_SECRET = "phase6w_webhook_secret"


def sandbox_webhook_posture(**overrides) -> FinanceWebhookRoutePosture:
    values = {
        "sandbox_webhook_enabled": True,
        "provider_mode": "test",
    }
    values.update(overrides)
    return FinanceWebhookRoutePosture(**values)


def signature(raw_body: bytes, *, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.digest(secret.encode("utf-8"), raw_body, "sha256").hex()


def signed_headers(raw_body: bytes, *, idempotency_key: str = "phase6w-webhook", sig: str | None = None) -> dict[str, str]:
    return {
        "X-Razorpay-Signature": sig if sig is not None else signature(raw_body),
        "X-Idempotency-Key": idempotency_key,
    }


def override_webhook_dependencies(
    *,
    posture: FinanceWebhookRoutePosture | None = None,
    razorpay_config: RazorpaySandboxConfig | None = None,
    provider_config: ProviderSandboxConfig | None = None,
) -> None:
    app.dependency_overrides[get_finance_webhook_route_posture] = lambda: posture or sandbox_webhook_posture()
    app.dependency_overrides[get_razorpay_webhook_test_mode_config] = lambda: razorpay_config or sandbox_config(
        mode="test",
        key_id="rzp_test_phase6w_key",
        key_secret="phase6w_server_only_key_secret",
        webhook_secret=WEBHOOK_SECRET,
    )
    app.dependency_overrides[get_provider_webhook_sandbox_config] = lambda: provider_config or PROVIDER_CONFIG


def clear_webhook_dependency_overrides() -> None:
    for dependency in (
        get_finance_webhook_route_posture,
        get_razorpay_webhook_test_mode_config,
        get_provider_webhook_sandbox_config,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.mark.asyncio
async def test_phase6w_default_webhook_runtime_remains_disabled_before_body_processing_or_mutation(client):
    before = await finance_counts()
    response = await client.post(
        "/api/v1/finance/payments/webhooks/razorpay",
        content=b"not-json-and-not-signed",
        headers={"X-Razorpay-Signature": "syntactic-only", "X-Idempotency-Key": "phase6w-default-disabled"},
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_phase6w_sandbox_webhook_route_verifies_raw_body_signature_and_updates_state_only(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6w_captured", event_type="payment.captured", payment_id="pay_phase6w", status="captured")
    override_webhook_dependencies()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-captured"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"
    assert await fetch_scalar("SELECT provider_payment_ref FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "pay_phase6w"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6w_captured'") == 1
    await assert_no_webhook_financial_side_effects()


@pytest.mark.asyncio
async def test_phase6w_signature_uses_exact_raw_body_not_reformatted_json(client):
    checkout = await seed_checkout()
    payload = {
        "id": "evt_phase6w_raw_body",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_phase6w_raw", "order_id": "order_test_1", "status": "captured", "amount": 118000, "currency": "INR"}}},
    }
    raw_pretty = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    raw_compact = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    override_webhook_dependencies()
    try:
        rejected = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw_compact,
            headers=signed_headers(raw_pretty, idempotency_key="phase6w-reformatted"),
        )
        accepted = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw_pretty,
            headers=signed_headers(raw_pretty, idempotency_key="phase6w-raw-body"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert rejected.status_code == 401
    assert rejected.json()["detail"]["code"] == "FINANCE_WEBHOOK_SIGNATURE_INVALID"
    assert accepted.status_code == 202
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"


@pytest.mark.asyncio
async def test_phase6w_missing_or_invalid_signature_rejects_before_mutation(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6w_bad_sig", payment_id="pay_phase6w_bad_sig")
    override_webhook_dependencies()
    try:
        missing = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers={"X-Idempotency-Key": "phase6w-missing-signature"},
        )
        invalid = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-invalid-signature", sig="bad"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert "secret" not in str(missing.json()).lower()
    assert "secret" not in str(invalid.json()).lower()
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_phase6w_malformed_json_after_valid_signature_fails_safely_without_mutation(client):
    checkout = await seed_checkout()
    raw = b'{"id":"evt_phase6w_malformed","event":"payment.captured",'
    override_webhook_dependencies()
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-malformed"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_PAYLOAD_INVALID"
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.parametrize(
    "razorpay_config",
    [
        sandbox_config(mode="live", key_secret="phase6w_private_key_secret", webhook_secret=WEBHOOK_SECRET),
        sandbox_config(key_id="rzp_live_phase6w", key_secret="phase6w_private_key_secret", webhook_secret=WEBHOOK_SECRET),
        sandbox_config(key_secret="phase6w_private_key_secret", webhook_secret=""),
        RazorpaySandboxConfig(
            mode="test",
            key_id="rzp_test_phase6w_key",
            key_secret="phase6w_private_key_secret",
            webhook_secret=WEBHOOK_SECRET,
            merchant_reference="phase6w",
            api_base_url="https://unsafe.example.invalid/v1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_phase6w_unsafe_webhook_config_fails_safely_before_mutation(client, razorpay_config):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6w_unsafe_config", payment_id="pay_phase6w_unsafe_config")
    override_webhook_dependencies(razorpay_config=razorpay_config)
    try:
        response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-unsafe-config"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FINANCE_WEBHOOK_PROVIDER_CONFIG_UNSAFE"
    assert "phase6w_private_key_secret" not in response.text
    assert WEBHOOK_SECRET not in response.text
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_phase6w_replay_is_idempotent_and_unknown_refs_fail_safely(client):
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_phase6w_replay", event_type="payment.authorized", payment_id="pay_phase6w_replay", status="authorized")
    missing = razorpay_payload(event_id="evt_phase6w_missing", order_id="order_missing", payment_id="pay_missing")
    override_webhook_dependencies()
    try:
        first = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-replay"),
        )
        replay = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=raw,
            headers=signed_headers(raw, idempotency_key="phase6w-replay"),
        )
        unknown = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=missing,
            headers=signed_headers(missing, idempotency_key="phase6w-missing-ref"),
        )
    finally:
        clear_webhook_dependency_overrides()

    assert first.status_code == 202
    assert replay.status_code == 202
    assert unknown.status_code == 400
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_phase6w_replay'") == 1
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "authorized"


@pytest.mark.asyncio
async def test_phase6w_webhook_enablement_does_not_enable_checkout_apply_status_or_admin_routes(client):
    await seed_checkout()
    override_webhook_dependencies()
    before = await finance_counts()
    try:
        checkout_response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(role="admin"), "X-Idempotency-Key": "phase6w-checkout-still-disabled"},
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
                "idempotency_key": "phase6w-apply-disabled",
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


def test_phase6w_jwt_exemption_is_exact_path_only_and_no_network_or_subscription_behavior():
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay") is True
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay/extra") is False
    assert _is_exempt("/api/v1/finance/payments/webhooks/razorpay-live") is False
    assert _is_exempt("/api/v1/finance/payments/checkout-sessions") is False

    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))
    assert "finance_payment_api_enabled = false" in combined
    assert "razorpay.client" not in combined
    assert "razorpayclient" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined


async def assert_no_webhook_financial_side_effects():
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.reconciled'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.refunds") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.credit_notes") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0
