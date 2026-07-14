from __future__ import annotations

from pathlib import Path

import pytest

from app.finance_core.api.guards import (
    get_finance_checkout_route_posture,
    get_finance_internal_apply_route_posture,
    get_finance_webhook_route_posture,
)
from app.finance_core.services.razorpay_local_smoke import (
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    RAZORPAY_PROVIDER_MODE,
    RAZORPAY_WEBHOOK_SECRET,
    RazorpayLocalSmokeConfigError,
    RazorpayLocalSmokeErrorCode,
    build_razorpay_local_smoke_plan,
    load_razorpay_test_mode_config_from_env,
    redact_razorpay_key_id,
)
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts
from tests.finance_core.test_phase6i_payment_api_auth_rbac_tenant_isolation import auth_headers


TEST_ENV = {
    RAZORPAY_PROVIDER_MODE: "test",
    RAZORPAY_KEY_ID: "rzp_test_phase6z_local_key",
    RAZORPAY_KEY_SECRET: "phase6z_private_key_secret",
    RAZORPAY_WEBHOOK_SECRET: "phase6z_private_webhook_secret",
}


def test_phase6z_local_loader_builds_safe_test_mode_config_from_explicit_env():
    config = load_razorpay_test_mode_config_from_env(TEST_ENV, require_webhook_secret=True)

    assert config.mode == "test"
    assert config.key_id == "rzp_test_phase6z_local_key"
    assert config.key_secret == "phase6z_private_key_secret"
    assert config.webhook_secret == "phase6z_private_webhook_secret"
    assert config.merchant_reference == "vitara_local_smoke"
    assert config.api_base_url == "https://api.razorpay.com/v1"


@pytest.mark.parametrize(
    ("missing_name", "expected_code"),
    [
        (RAZORPAY_PROVIDER_MODE, RazorpayLocalSmokeErrorCode.MISSING_PROVIDER_MODE),
        (RAZORPAY_KEY_ID, RazorpayLocalSmokeErrorCode.MISSING_KEY_ID),
        (RAZORPAY_KEY_SECRET, RazorpayLocalSmokeErrorCode.MISSING_KEY_SECRET),
        (RAZORPAY_WEBHOOK_SECRET, RazorpayLocalSmokeErrorCode.MISSING_WEBHOOK_SECRET),
    ],
)
def test_phase6z_local_loader_missing_values_fail_safely_without_leaking_secret(missing_name: str, expected_code: RazorpayLocalSmokeErrorCode):
    env = dict(TEST_ENV)
    env.pop(missing_name)

    with pytest.raises(RazorpayLocalSmokeConfigError) as exc:
        load_razorpay_test_mode_config_from_env(env, require_webhook_secret=True)

    assert exc.value.code == expected_code
    rendered = str(exc.value)
    assert "phase6z_private_key_secret" not in rendered
    assert "phase6z_private_webhook_secret" not in rendered


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({RAZORPAY_PROVIDER_MODE: "live"}, RazorpayLocalSmokeErrorCode.UNSAFE_PROVIDER_MODE),
        ({RAZORPAY_PROVIDER_MODE: "production"}, RazorpayLocalSmokeErrorCode.UNSAFE_PROVIDER_MODE),
        ({RAZORPAY_KEY_ID: "rzp_live_phase6z_key"}, RazorpayLocalSmokeErrorCode.LIVE_KEY_REJECTED),
        ({RAZORPAY_KEY_SECRET: "rzp_live_phase6z_secret"}, RazorpayLocalSmokeErrorCode.LIVE_KEY_REJECTED),
        ({RAZORPAY_KEY_ID: "not_a_razorpay_test_key"}, RazorpayLocalSmokeErrorCode.UNSAFE_CONFIG),
    ],
)
def test_phase6z_local_loader_rejects_live_or_unsafe_material(overrides: dict[str, str], expected_code: RazorpayLocalSmokeErrorCode):
    env = {**TEST_ENV, **overrides}

    with pytest.raises(RazorpayLocalSmokeConfigError) as exc:
        load_razorpay_test_mode_config_from_env(env, require_webhook_secret=True)

    assert exc.value.code == expected_code
    rendered = str(exc.value)
    assert "phase6z_private_key_secret" not in rendered
    assert "phase6z_private_webhook_secret" not in rendered


def test_phase6z_webhook_secret_is_required_only_for_webhook_readiness():
    env = dict(TEST_ENV)
    env.pop(RAZORPAY_WEBHOOK_SECRET)

    config = load_razorpay_test_mode_config_from_env(env, require_webhook_secret=False)

    assert config.mode == "test"
    assert config.webhook_secret == "__dry_run_webhook_secret_not_for_network__"


def test_phase6z_redaction_and_dry_run_plan_print_no_secrets():
    plan = build_razorpay_local_smoke_plan(environ=TEST_ENV, require_webhook_secret=True)
    safe = plan.to_safe_output()
    rendered = str(safe)

    assert safe["readiness"]["provider_mode"] == "test"
    assert safe["readiness"]["key_id"] == "[REDACTED]..._key"
    assert safe["readiness"]["key_secret_present"] is True
    assert safe["readiness"]["webhook_secret_present"] is True
    assert safe["readiness"]["payment_route_defaults_disabled"] is True
    assert safe["readiness"]["explicit_test_overrides_required"] is True
    assert safe["readiness"]["dry_run"] is True
    assert safe["readiness"]["network_execution_allowed"] is False
    assert "phase6z_private_key_secret" not in rendered
    assert "phase6z_private_webhook_secret" not in rendered
    assert "rzp_test_phase6z_local_key" not in rendered


def test_phase6z_harness_refuses_network_execution_in_this_phase():
    with pytest.raises(RazorpayLocalSmokeConfigError) as exc:
        build_razorpay_local_smoke_plan(environ=TEST_ENV, allow_test_network=True)

    assert exc.value.code == RazorpayLocalSmokeErrorCode.TEST_NETWORK_NOT_ALLOWED
    assert "phase6z_private_key_secret" not in str(exc.value)


def test_phase6z_loader_is_not_called_by_route_posture_defaults():
    checkout = get_finance_checkout_route_posture()
    webhook = get_finance_webhook_route_posture()
    internal_apply = get_finance_internal_apply_route_posture()

    assert checkout.sandbox_checkout_enabled is False
    assert webhook.sandbox_webhook_enabled is False
    assert internal_apply.sandbox_internal_apply_enabled is False
    assert checkout.provider_mode == "disabled"
    assert webhook.provider_mode == "disabled"
    assert internal_apply.provider_mode == "disabled"


@pytest.mark.asyncio
async def test_phase6z_route_defaults_remain_disabled_and_do_not_mutate_finance_state(client):
    before = await finance_counts()
    checkout = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers={**auth_headers(role="admin"), "X-Idempotency-Key": "phase6z-disabled-checkout"},
        json={"plan_code": "DOERS_PRO_MONTHLY", "billing_interval": "monthly", "billing_party_id": "00000000-0000-0000-0000-000000000001"},
    )
    webhook = await client.post(
        "/api/v1/finance/payments/webhooks/razorpay",
        content=b"{}",
        headers={"X-Razorpay-Signature": "syntactic-only", "X-Idempotency-Key": "phase6z-disabled-webhook"},
    )
    internal_apply = await client.post(
        "/api/v1/finance/payments/internal/payment-applications",
        headers={**auth_headers(role="admin"), "X-Finance-Internal-Actor": "finance_core"},
        json={
            "payment_id": "00000000-0000-0000-0000-000000000001",
            "invoice_id": "00000000-0000-0000-0000-000000000002",
            "amount": "1.00",
            "currency_code": "INR",
            "idempotency_key": "phase6z-disabled-apply",
            "internal_actor": "finance_core",
            "reason": "route defaults remain disabled",
        },
    )

    assert_disabled(checkout)
    assert_disabled(webhook)
    assert_disabled(internal_apply)
    assert await finance_counts() == before


def test_phase6z_no_credentials_examples_frontend_network_client_or_subscription_behavior_added():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert redact_razorpay_key_id("rzp_test_phase6z_local_key") == "[REDACTED]..._key"
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
    assert not (repo_root / "frontend").exists()
