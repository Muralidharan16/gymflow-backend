from __future__ import annotations

import hmac
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from app.core.security import (
    create_access_token,
    create_refresh_token,
    finance_csrf_token,
    verify_finance_csrf_token,
    verify_token,
)
from app.finance_core.domain.provider_boundary import (
    FinanceWebhookNormalizationError,
    ProviderSandboxConfig,
)
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput
from app.finance_core.security_abuse import (
    RECENT_AUTH_WINDOW_SECONDS,
    _validated_recent_auth,
    enforce_maker_checker,
    enforce_secure_export,
    reject_payment_secret_fields,
    validate_admin_reason_code,
)
from app.finance_core.services.razorpay_webhooks import (
    MAX_RAZORPAY_FUTURE_SKEW_SECONDS,
    RazorpayWebhookConfirmationService,
)


def _http_code(exc: HTTPException) -> str:
    detail = exc.detail
    assert isinstance(detail, dict)
    return str(detail["code"])


def test_access_and_refresh_tokens_bind_session_family_and_preserve_auth_time():
    auth_time = int(time.time()) - 30
    family_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    access = create_access_token(
        actor_id,
        org_id,
        "owner@pay16.test",
        role="owner",
        auth_time=auth_time,
        family_id=family_id,
    )
    refresh = create_refresh_token(
        actor_id,
        auth_time=auth_time,
        family_id=family_id,
    )

    access_claims = verify_token(access, "access")
    refresh_claims = verify_token(refresh, "refresh")

    assert access_claims["auth_time"] == auth_time
    assert refresh_claims["auth_time"] == auth_time
    assert access_claims["f_id"] == family_id
    assert refresh_claims["f_id"] == family_id
    assert isinstance(access_claims["iat"], int)
    assert isinstance(refresh_claims["iat"], int)
    assert access_claims["jti"] != refresh_claims["jti"]


def test_finance_csrf_proof_is_bound_to_access_token_jti():
    access = create_access_token(
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "owner@pay16.test",
        auth_time=int(time.time()),
    )
    other = create_access_token(
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "other@pay16.test",
        auth_time=int(time.time()),
    )

    proof = finance_csrf_token(access)
    assert len(proof) == 64
    assert verify_finance_csrf_token(access, proof) is True
    assert verify_finance_csrf_token(access, "0" * 64) is False
    assert verify_finance_csrf_token(other, proof) is False


@pytest.mark.parametrize(
    ("auth_time", "expected_code"),
    [
        (1000 - RECENT_AUTH_WINDOW_SECONDS - 1, "FINANCE_STEP_UP_REQUIRED"),
        (1000 + 61, "FINANCE_STEP_UP_REQUIRED"),
    ],
)
def test_recent_auth_rejects_stale_or_impossibly_future_sessions(auth_time, expected_code):
    with pytest.raises(HTTPException) as captured:
        _validated_recent_auth({"auth_time": auth_time}, now_epoch=1000)
    assert _http_code(captured.value) == expected_code


def test_recent_auth_accepts_exact_window_boundary():
    assert (
        _validated_recent_auth(
            {"auth_time": 1000 - RECENT_AUTH_WINDOW_SECONDS},
            now_epoch=1000,
        )
        == 1000 - RECENT_AUTH_WINDOW_SECONDS
    )


def test_maker_checker_refuses_self_approval():
    actor = uuid.uuid4()
    with pytest.raises(HTTPException) as captured:
        enforce_maker_checker(maker_actor_id=actor, checker_actor_id=actor)
    assert captured.value.status_code == 409
    assert _http_code(captured.value) == "FINANCE_MAKER_CHECKER_REQUIRED"

    enforce_maker_checker(
        maker_actor_id=actor,
        checker_actor_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("manual_bank_proof", "MANUAL_BANK_PROOF"),
        ("OFFLINE:CASH.REVIEW", "OFFLINE:CASH.REVIEW"),
    ],
)
def test_administrative_reason_codes_are_bounded_and_normalized(value, expected):
    assert validate_admin_reason_code(value) == expected


@pytest.mark.parametrize("value", [None, "", "x", "contains spaces", "a" * 81])
def test_invalid_administrative_reason_codes_fail_closed(value):
    with pytest.raises(HTTPException) as captured:
        validate_admin_reason_code(value)
    assert captured.value.status_code == 400
    assert _http_code(captured.value) == "FINANCE_REASON_CODE_REQUIRED"


def test_secure_export_blocks_payment_authentication_material_and_large_exports():
    enforce_secure_export(requested_rows=100, fields={"invoice_id", "amount"})

    with pytest.raises(HTTPException) as captured:
        enforce_secure_export(requested_rows=100, fields={"invoice_id", "cvv"})
    assert _http_code(captured.value) == "FINANCE_EXPORT_SENSITIVE_FIELD_FORBIDDEN"

    with pytest.raises(HTTPException) as captured:
        enforce_secure_export(requested_rows=5001, fields={"invoice_id"})
    assert _http_code(captured.value) == "FINANCE_EXPORT_LIMIT_EXCEEDED"


@pytest.mark.parametrize("field", ["pan", "card_number", "cvv", "cvc", "upi_pin"])
def test_pan_cvv_and_upi_pin_storage_contract_is_fail_closed(field):
    with pytest.raises(HTTPException) as captured:
        reject_payment_secret_fields({field})
    assert _http_code(captured.value) == "FINANCE_PAYMENT_SECRET_STORAGE_FORBIDDEN"


def _provider_config() -> ProviderSandboxConfig:
    return ProviderSandboxConfig(
        provider_code="razorpay_sandbox",
        sandbox_mode=True,
        merchant_id="pay16-merchant",
        signing_secret="pay16-provider-signing",
    )


def _razorpay_config(**overrides) -> RazorpaySandboxConfig:
    values = {
        "mode": "test",
        "key_id": "rzp_test_pay16",
        "key_secret": "pay16-key-secret",
        "webhook_secret": "pay16-current-webhook-secret",
        "previous_webhook_secret": "pay16-previous-webhook-secret",
        "merchant_reference": "pay16-merchant",
    }
    values.update(overrides)
    return RazorpaySandboxConfig(**values)


def _webhook_payload(*, created_at: int | None = None) -> bytes:
    payload = {
        "event": "payment.authorized",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_pay16",
                    "order_id": "order_pay16",
                    "amount": 118000,
                    "currency": "INR",
                    "status": "authorized",
                    "captured": False,
                }
            }
        },
    }
    if created_at is not None:
        payload["created_at"] = created_at
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _normalizer(config: RazorpaySandboxConfig) -> RazorpayWebhookConfirmationService:
    return RazorpayWebhookConfirmationService(
        None,
        razorpay_config=config,
        provider_config=_provider_config(),
    )


def test_webhook_secret_rotation_accepts_current_and_previous_but_not_unknown_secret():
    raw = _webhook_payload(created_at=int(time.time()))
    service = _normalizer(_razorpay_config())

    for secret in (
        b"pay16-current-webhook-secret",
        b"pay16-previous-webhook-secret",
    ):
        signature = hmac.digest(secret, raw, "sha256").hex()
        reference = service.normalize(
            RazorpayWebhookInput(
                raw_body=raw,
                signature=signature,
                provider_event_id="evt_pay16_rotation",
            )
        )
        assert reference.signature_verified is True

    bad_signature = hmac.digest(b"unknown-secret", raw, "sha256").hex()
    with pytest.raises(Exception):
        service.normalize(
            RazorpayWebhookInput(
                raw_body=raw,
                signature=bad_signature,
                provider_event_id="evt_pay16_bad_secret",
            )
        )


def test_webhook_rejects_future_timestamp_beyond_skew_but_allows_old_provider_retry():
    now = int(time.time())
    service = _normalizer(_razorpay_config())

    future = _webhook_payload(
        created_at=now + MAX_RAZORPAY_FUTURE_SKEW_SECONDS + 1
    )
    future_signature = hmac.digest(
        b"pay16-current-webhook-secret",
        future,
        "sha256",
    ).hex()
    with pytest.raises(FinanceWebhookNormalizationError):
        service.normalize(
            RazorpayWebhookInput(
                raw_body=future,
                signature=future_signature,
                provider_event_id="evt_pay16_future",
            )
        )

    old = _webhook_payload(created_at=now - 30 * 24 * 60 * 60)
    old_signature = hmac.digest(
        b"pay16-current-webhook-secret",
        old,
        "sha256",
    ).hex()
    assert service.normalize(
        RazorpayWebhookInput(
            raw_body=old,
            signature=old_signature,
            provider_event_id="evt_pay16_old_retry",
        )
    ).provider_event_id == "evt_pay16_old_retry"
