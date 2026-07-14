from __future__ import annotations

import hmac
from pathlib import Path

import pytest

from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutSignatureError,
    RazorpaySandboxConfig,
    verify_razorpay_checkout_signature,
    verify_razorpay_webhook_signature,
)
from app.finance_core.services.razorpay_checkout import (
    RazorpayCheckoutSignatureVerificationCommand,
    RazorpayCheckoutSignatureVerificationService,
)
from tests.finance_core.test_phase5c_invoice_engine import fetch_one
from tests.finance_core.test_phase5d_payment_ledger import seed_finance_foundation


KEY_SECRET = "phase6ag_private_key_secret"
WEBHOOK_SECRET = "phase6ag_private_webhook_secret"
ORDER_ID = "order_phase6ag_test"
PAYMENT_ID = "pay_phase6ag_test"


def sandbox_config(*, key_secret: str = KEY_SECRET, webhook_secret: str = WEBHOOK_SECRET) -> RazorpaySandboxConfig:
    return RazorpaySandboxConfig(
        mode="test",
        key_id="rzp_test_phase6ag",
        key_secret=key_secret,
        webhook_secret=webhook_secret,
        merchant_reference="vitara_phase6ag",
    )


def checkout_signature(*, key_secret: str = KEY_SECRET, order_id: str = ORDER_ID, payment_id: str = PAYMENT_ID) -> str:
    return hmac.digest(key_secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), "sha256").hex()


def test_valid_checkout_signature_passes_server_side_verification():
    service = RazorpayCheckoutSignatureVerificationService(config=sandbox_config())

    result = service.verify(
        RazorpayCheckoutSignatureVerificationCommand(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=checkout_signature(),
        )
    )

    assert result.verified is True
    assert result.provider_order_id == ORDER_ID
    assert result.provider_payment_id == PAYMENT_ID
    assert KEY_SECRET not in repr(result)
    assert WEBHOOK_SECRET not in repr(result)


def test_invalid_checkout_signature_fails_without_echoing_secret_or_signature():
    supplied_signature = "bad_phase6ag_signature"

    with pytest.raises(RazorpayCheckoutSignatureError) as exc:
        verify_razorpay_checkout_signature(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=supplied_signature,
            key_secret=KEY_SECRET,
        )

    rendered = str(exc.value)
    assert exc.value.code == "RAZORPAY_CHECKOUT_SIGNATURE_INVALID"
    assert KEY_SECRET not in rendered
    assert supplied_signature not in rendered
    assert ORDER_ID not in rendered
    assert PAYMENT_ID not in rendered


@pytest.mark.parametrize(
    ("order_id", "payment_id", "signature", "key_secret", "code"),
    [
        ("", PAYMENT_ID, "sig", KEY_SECRET, "RAZORPAY_CHECKOUT_ORDER_ID_REQUIRED"),
        ("   ", PAYMENT_ID, "sig", KEY_SECRET, "RAZORPAY_CHECKOUT_ORDER_ID_REQUIRED"),
        (ORDER_ID, "", "sig", KEY_SECRET, "RAZORPAY_CHECKOUT_PAYMENT_ID_REQUIRED"),
        (ORDER_ID, "   ", "sig", KEY_SECRET, "RAZORPAY_CHECKOUT_PAYMENT_ID_REQUIRED"),
        (ORDER_ID, PAYMENT_ID, "", KEY_SECRET, "RAZORPAY_CHECKOUT_SIGNATURE_REQUIRED"),
        (ORDER_ID, PAYMENT_ID, "   ", KEY_SECRET, "RAZORPAY_CHECKOUT_SIGNATURE_REQUIRED"),
        (ORDER_ID, PAYMENT_ID, "sig", "", "RAZORPAY_CHECKOUT_KEY_SECRET_REQUIRED"),
        (ORDER_ID, PAYMENT_ID, "sig", "   ", "RAZORPAY_CHECKOUT_KEY_SECRET_REQUIRED"),
    ],
)
def test_missing_or_whitespace_checkout_fields_are_rejected(order_id, payment_id, signature, key_secret, code):
    with pytest.raises(RazorpayCheckoutSignatureError) as exc:
        verify_razorpay_checkout_signature(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature,
            key_secret=key_secret,
        )

    assert exc.value.code == code
    rendered = str(exc.value)
    assert KEY_SECRET not in rendered
    assert WEBHOOK_SECRET not in rendered
    assert "bad_phase6ag_signature" not in rendered


def test_checkout_verification_uses_key_secret_not_webhook_secret():
    valid_with_key_secret = checkout_signature(key_secret=KEY_SECRET)
    invalid_with_webhook_secret = checkout_signature(key_secret=WEBHOOK_SECRET)

    assert (
        verify_razorpay_checkout_signature(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=valid_with_key_secret,
            key_secret=KEY_SECRET,
        ).verified
        is True
    )

    with pytest.raises(RazorpayCheckoutSignatureError):
        verify_razorpay_checkout_signature(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=invalid_with_webhook_secret,
            key_secret=KEY_SECRET,
        )


def test_webhook_raw_body_verifier_remains_separate_and_unchanged():
    raw_body = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_phase6ag"}}}}'
    signature = hmac.digest(WEBHOOK_SECRET.encode("utf-8"), raw_body, "sha256").hex()

    assert verify_razorpay_webhook_signature(raw_body=raw_body, signature=signature, webhook_secret=WEBHOOK_SECRET) is True
    assert verify_razorpay_webhook_signature(raw_body=raw_body, signature=checkout_signature(), webhook_secret=WEBHOOK_SECRET) is False


def test_checkout_signature_verification_uses_constant_time_compare(monkeypatch):
    calls: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def tracked_compare(expected: str, supplied: str) -> bool:
        calls.append((expected, supplied))
        return original(expected, supplied)

    monkeypatch.setattr("app.finance_core.domain.razorpay_sandbox.hmac.compare_digest", tracked_compare)

    result = verify_razorpay_checkout_signature(
        razorpay_order_id=ORDER_ID,
        razorpay_payment_id=PAYMENT_ID,
        razorpay_signature=checkout_signature(),
        key_secret=KEY_SECRET,
    )

    assert result.verified is True
    assert calls == [(checkout_signature(), checkout_signature())]


def test_key_secret_is_not_present_in_repr_str_errors_or_results():
    service = RazorpayCheckoutSignatureVerificationService(config=sandbox_config())
    result = service.verify(
        RazorpayCheckoutSignatureVerificationCommand(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=checkout_signature(),
        )
    )

    rendered = "\n".join([repr(service), repr(result), str(result)])
    assert KEY_SECRET not in rendered
    assert WEBHOOK_SECRET not in rendered
    assert "Authorization" not in rendered


@pytest.mark.asyncio
async def test_checkout_signature_verification_does_not_mutate_finance_state():
    await seed_finance_foundation()
    before = await _finance_counts()

    service = RazorpayCheckoutSignatureVerificationService(config=sandbox_config())
    service.verify(
        RazorpayCheckoutSignatureVerificationCommand(
            razorpay_order_id=ORDER_ID,
            razorpay_payment_id=PAYMENT_ID,
            razorpay_signature=checkout_signature(),
        )
    )

    after = await _finance_counts()
    assert after == before


async def _finance_counts():
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payments) AS payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.ledger_entry_lines) AS ledger_lines,
            (SELECT count(*) FROM finance.invoices WHERE status = 'paid') AS paid_invoices,
            (SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions') AS subscription_tables
        """
    )


def test_phase6ag_adds_no_routes_frontend_network_or_provider_side_effects():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "razorpaycheckoutsignatureverificationservice" in combined
    assert "http.client" in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert "provider_secret" not in combined
    assert "rzp_live_" not in combined
    assert not (repo_root / "frontend").exists()
