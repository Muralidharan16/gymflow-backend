from __future__ import annotations

import hmac
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.operational_guards import FinanceOperationalGuardError, FinanceOperationalPosture
from app.finance_core.domain.provider_boundary import (
    FinancePaymentStateTransitionError,
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    ProviderSandboxConfig,
)
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.razorpay_webhooks import RazorpayWebhookConfirmationService
from tests.finance_core.test_phase5c_invoice_engine import fetch_one, fetch_scalar, seed_master_data
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6c_checkout_orchestration import command, orchestrate


PROVIDER_CONFIG = ProviderSandboxConfig(
    provider_code="razorpay_sandbox",
    sandbox_mode=True,
    merchant_id="merchant_test",
    signing_secret="provider-signing-secret",
)


def razorpay_payload(
    *,
    event_id: str = "evt_payment_authorized",
    event_type: str = "payment.authorized",
    payment_id: str = "pay_test_1",
    order_id: str = "order_test_1",
    status: str = "authorized",
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "event": event_type,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "status": status,
                        "amount": 118000,
                        "currency": "INR",
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def signed_webhook(raw_body: bytes, *, idempotency_key: str | None = None, signature: str | None = None) -> RazorpayWebhookInput:
    return RazorpayWebhookInput(
        raw_body=raw_body,
        signature=signature if signature is not None else hmac.digest(b"rzp_webhook_secret", raw_body, "sha256").hex(),
        idempotency_key=idempotency_key,
    )


async def seed_checkout():
    await seed_master_data()
    result, _client = await orchestrate(command(idempotency_key="phase6d-checkout"))
    return result


async def confirm(webhook: RazorpayWebhookInput, *, guard=None):
    async with AsyncSessionLocal() as session:
        service = RazorpayWebhookConfirmationService(
            session,
            razorpay_config=sandbox_config(webhook_secret="rzp_webhook_secret"),
            provider_config=PROVIDER_CONFIG,
            guard_service=guard,
        )
        result = await service.confirm_payment_event(webhook)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_valid_payment_authorized_webhook_updates_payment_state_to_authorized():
    checkout = await seed_checkout()
    result = await confirm(signed_webhook(razorpay_payload(), idempotency_key="evt-authorized"))

    assert result.payment_id == checkout.finance_checkout_intent_id
    assert result.payment_status == "authorized"
    assert result.state_applied is True
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "authorized"
    assert await fetch_scalar("SELECT provider_payment_ref FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "pay_test_1"


@pytest.mark.asyncio
async def test_valid_payment_captured_webhook_updates_payment_state_to_captured_without_side_effects():
    checkout = await seed_checkout()
    result = await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_payment_captured", event_type="payment.captured", payment_id="pay_test_2", status="captured"),
            idempotency_key="evt-captured",
        )
    )

    assert result.payment_id == checkout.finance_checkout_intent_id
    assert result.payment_status == "captured"
    assert result.state_applied is True
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"
    await assert_no_financial_side_effects()


@pytest.mark.asyncio
async def test_valid_payment_failed_webhook_updates_payment_state_to_failed():
    checkout = await seed_checkout()
    result = await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_payment_failed", event_type="payment.failed", payment_id="pay_test_3", status="failed"),
            idempotency_key="evt-failed",
        )
    )

    assert result.payment_id == checkout.finance_checkout_intent_id
    assert result.payment_status == "failed"
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "failed"


@pytest.mark.asyncio
async def test_invalid_or_missing_signature_fails_before_mutation():
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_bad_signature")

    with pytest.raises(FinanceWebhookSignatureError):
        await confirm(signed_webhook(raw, idempotency_key="bad-signature", signature="bad"))
    with pytest.raises(FinanceWebhookSignatureError):
        await confirm(RazorpayWebhookInput(raw_body=raw, signature=None, idempotency_key="missing-signature"))

    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_unknown_event_type_and_unknown_provider_order_fail_safely_without_mutation():
    await seed_checkout()
    with pytest.raises(FinanceWebhookNormalizationError):
        await confirm(
            signed_webhook(
                razorpay_payload(event_id="evt_unknown_type", event_type="payment.mystery", status="mystery"),
                idempotency_key="unknown-type",
            )
        )
    with pytest.raises(FinanceWebhookNormalizationError):
        await confirm(
            signed_webhook(
                razorpay_payload(event_id="evt_unknown_order", order_id="order_missing", payment_id="pay_missing"),
                idempotency_key="unknown-order",
            )
        )

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_duplicate_replay_is_idempotent_and_changed_payload_conflicts():
    checkout = await seed_checkout()
    raw = razorpay_payload(event_id="evt_replay", event_type="payment.authorized", payment_id="pay_replay", status="authorized")
    first = await confirm(signed_webhook(raw, idempotency_key="evt-replay"))
    replay = await confirm(signed_webhook(raw, idempotency_key="evt-replay"))

    assert replay.replayed is True
    assert replay.payment_event_id == first.payment_event_id
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events WHERE provider_event_id = 'evt_replay'") == 1
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "authorized"

    with pytest.raises(Exception):
        await confirm(
            signed_webhook(
                razorpay_payload(event_id="evt_replay", event_type="payment.captured", payment_id="pay_replay", status="captured"),
                idempotency_key="evt-replay-conflict",
            )
        )


@pytest.mark.asyncio
async def test_out_of_order_lower_state_does_not_downgrade_captured_payment():
    checkout = await seed_checkout()
    await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_capture_first", event_type="payment.captured", payment_id="pay_stale", status="captured"),
            idempotency_key="capture-first",
        )
    )
    stale = await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_authorized_late", event_type="payment.authorized", payment_id="pay_stale", status="authorized"),
            idempotency_key="authorized-late",
        )
    )

    assert stale.state_ignored is True
    assert stale.payment_status == "captured"
    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "captured"


@pytest.mark.asyncio
async def test_terminal_failed_payment_rejects_later_captured_transition():
    await seed_checkout()
    await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_failed_terminal", event_type="payment.failed", payment_id="pay_terminal", status="failed"),
            idempotency_key="failed-terminal",
        )
    )
    with pytest.raises(FinancePaymentStateTransitionError):
        await confirm(
            signed_webhook(
                razorpay_payload(event_id="evt_captured_after_failed", event_type="payment.captured", payment_id="pay_terminal", status="captured"),
                idempotency_key="captured-after-failed",
            )
        )


@pytest.mark.asyncio
async def test_live_provider_posture_blocks_before_mutation():
    checkout = await seed_checkout()
    guard = FinanceOperationalGuardService(FinanceOperationalPosture(live_provider_enabled=True))

    with pytest.raises(FinanceOperationalGuardError):
        await confirm(signed_webhook(razorpay_payload(event_id="evt_live_block"), idempotency_key="live-block"), guard=guard)

    assert await fetch_scalar("SELECT status FROM finance.payments WHERE id = :id", {"id": checkout.finance_checkout_intent_id}) == "created"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_webhook_confirmation_does_not_allocate_post_ledger_mark_paid_reconcile_refund_or_activate_subscription():
    await seed_checkout()
    await confirm(
        signed_webhook(
            razorpay_payload(event_id="evt_no_side_effect", event_type="payment.captured", payment_id="pay_no_side_effect", status="captured"),
            idempotency_key="no-side-effect",
        )
    )

    await assert_no_financial_side_effects()


async def assert_no_financial_side_effects():
    row = await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.invoices WHERE status = 'paid') AS paid_invoices,
            (SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.reconciled') AS reconciliations,
            (SELECT count(*) FROM finance.refunds) AS refunds,
            (SELECT count(*) FROM finance.credit_notes) AS credit_notes,
            (SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions') AS subscription_tables
        """
    )
    assert row["allocations"] == 0
    assert row["ledger_entries"] == 0
    assert row["paid_invoices"] == 0
    assert row["reconciliations"] == 0
    assert row["refunds"] == 0
    assert row["credit_notes"] == 0
    assert row["subscription_tables"] >= 0


def test_phase6d_has_no_public_api_frontend_network_or_subscription_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "require_finance_payment_api_enabled" in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "rzp_live_" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
