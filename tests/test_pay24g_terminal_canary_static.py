from __future__ import annotations

import ast
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/pay24g_terminal_payment_canary.py"


def _text():
    return SCRIPT.read_text(encoding="utf-8")


def test_pay24g_real_money_canary_is_terminal_loopback_only() -> None:
    source=_text()
    ast.parse(source)
    assert 'HTTPServer(("127.0.0.1", 0)' in source
    assert "checkout.razorpay.com/v1/checkout.js" in source
    assert "_CALLBACK_TIMEOUT_SECONDS = 600" in source
    assert "PAY-24-G real-money command requires an interactive terminal" in source
    assert "AUTHORIZE LIVE PAYMENT INR" in source


def test_pay24g_hard_caps_amount_and_currency() -> None:
    source=_text()
    assert '_MAX_CANARY_AMOUNT = Decimal("10.00")' in source
    assert '_CANARY_CURRENCY = "INR"' in source
    assert "canary amount exceeds the INR 10.00 hard limit" in source


def test_pay24g_durable_admission_precedes_checkout_page() -> None:
    source=_text()
    request=source.index("request_current_provider_admission(")
    start=source.index("start_provider_admission(",request)
    commit=source.index("await payment_session.commit()",start)
    browser=source.index("_collect_callback,",commit)
    assert request < start < commit < browser


def test_pay24g_no_capture_or_refund_provider_api_exists() -> None:
    source=_text().lower()
    assert "/capture" not in source
    assert "/refund" not in source
    assert "capture_payment" not in source
    assert "submit_refund" not in source


def test_pay24g_reconciliation_identity_is_peer_isolated() -> None:
    source=_text()
    assert "finance_reconciliation_runtime" in source
    for role in (
        "app_runtime",
        "finance_payment_runtime",
        "finance_refund_runtime",
        "entitlement_runtime",
    ):
        assert role in source
    assert "reconciliation login reaches forbidden peer role" in source


def test_pay24g_internal_application_uses_existing_finance_gate() -> None:
    source=_text()
    assert "FinancePaymentApplicationGateService" in source
    assert "ApplyConfirmedPaymentCommand" in source
    assert 'internal_actor="ops_admin"' in source
    assert "PAY-24-G internal live payment canary" in source

def test_pay24g_callback_is_bound_to_exact_prepared_order() -> None:
    source=_text()
    assert 'callback.payload["razorpay_order_id"]' in source
    assert "!= prepared.provider_order_ref" in source
    assert "callback order does not match the prepared canary order" in source


def test_pay24g_cli_accepts_only_run_live_payment() -> None:
    source=_text()
    assert 'choices=("run-live-payment",)' in source

