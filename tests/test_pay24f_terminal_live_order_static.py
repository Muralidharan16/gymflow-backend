from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/pay24f_terminal_canary.py"
LIVE_DOMAIN = ROOT / "app/finance_core/domain/razorpay_live.py"
LIVE_SERVICE = ROOT / "app/finance_core/services/razorpay_live.py"
RELEASE = ROOT / "app/payment_activation/release_identity.py"
MIGRATION = ROOT / "alembic/versions/zzd7d8e9f0a73_pay24f_live_checkout_environment.py"
GUARDS = ROOT / "app/finance_core/api/guards.py"
MEMBER_ROUTE = ROOT / "app/routers/member_subscriptions_v2.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay24f_controller_has_separate_human_gates() -> None:
    source = _text(CONTROLLER)
    ast.parse(source)
    assert "activate-stage1" in source
    assert "prepare-live-order" in source
    assert "execute-live-order" in source
    assert "rollback-stage0" in source
    assert "AUTHORIZE PAY24 STAGE1" in source
    assert "CREATE LIVE RAZORPAY ORDER" in source
    assert "BEGIN PAY24 EMERGENCY ROLLBACK" in source
    assert "isatty()" in source


def test_pay24f_controller_cannot_accept_deployed_sha_from_operator() -> None:
    source = _text(CONTROLLER)
    release = _text(RELEASE)
    assert "--deployed-sha" not in source
    assert "deployed_sha=args" not in source
    assert "RootOwnedReleaseAttestationProvider" in source
    assert "st_uid != 0" in release
    assert "S_ISLNK" in release
    assert "S_IWGRP" in release
    assert "S_IWOTH" in release


def test_pay24f_live_adapter_is_order_only() -> None:
    service = _text(LIVE_SERVICE)
    start = service.index("class RazorpayLiveOrdersClient:")
    end = service.index("class RazorpayLivePaymentsClient:", start)
    order_client = service[start:end].lower()
    adapter_start = service.index("class RazorpayLiveCheckoutAdapter:")
    adapter = service[adapter_start:].lower()
    combined = order_client + "\n" + adapter
    assert "create_checkout_intent" in combined
    assert "/orders" in combined
    for forbidden in (
        "/capture",
        "/payments/",
        "submit_refund(",
        "fetch_refund(",
        "verify_webhook",
        "process_webhook",
        "apply_confirmed_payment",
    ):
        assert forbidden not in combined


def test_pay24f_live_secrets_are_not_returned_or_logged() -> None:
    domain = _text(LIVE_DOMAIN)
    service = _text(LIVE_SERVICE)
    assert '"key_secret": "[REDACTED]"' in domain
    assert "print(" not in service
    assert "logger." not in service


def test_pay24f_migration_widens_checkout_environment_only() -> None:
    source = _text(MIGRATION)
    assert 'revision = "zzd7d8e9f0a73"' in source
    assert 'down_revision = "zzc7d8e9f0a72"' in source
    assert "finance.provider_operations" in source
    assert "('sandbox','test','live')" in source
    assert "finance.provider_webhook_inbox" not in source
    assert "downgrade refused while live provider operations exist" in source


def test_existing_http_routes_remain_non_live() -> None:
    guards = _text(GUARDS)
    route = _text(MEMBER_ROUTE)
    assert "require_finance_checkout_sandbox_enabled" in route
    assert "RazorpayTestModeOrdersClient" in route
    assert "RazorpaySandboxAdapter" in route
    assert "RazorpayLiveCheckoutAdapter" not in route
    assert "live_money_movement_enabled" in guards


def test_execute_live_order_keeps_pay24b_admission_before_https() -> None:
    source = _text(CONTROLLER)
    claim = source.index("claim_provider_operation(")
    admission = source.index("request_current_provider_admission(", claim)
    first_commit = source.index("await payment.commit()", admission)
    start = source.index("start_provider_admission(", first_commit)
    second_commit = source.index("await payment.commit()", start)
    provider = source.index("adapter.create_checkout_intent(", second_commit)
    assert claim < admission < first_commit < start < second_commit < provider


def test_pay24f_controller_explicitly_reports_zero_money_movement() -> None:
    source = _text(CONTROLLER)
    assert '"real_money_movement": False' in source
    assert '"payment_capture_enabled": False' in source
    assert '"webhook_live_path_enabled": False' in source
