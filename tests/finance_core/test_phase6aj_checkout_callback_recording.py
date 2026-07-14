from __future__ import annotations

import hmac
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.checkout_callbacks import (
    FinanceCheckoutCallbackError,
    RecordCheckoutCallbackCommand,
)
from app.finance_core.domain.payment_ledger import FinancePaymentConflictError
from app.finance_core.domain.razorpay_sandbox import RazorpayCheckoutSignatureError
from app.finance_core.services.checkout_callbacks import FinanceCheckoutCallbackRecordingService
from app.finance_core.services.razorpay_webhooks import RazorpayWebhookConfirmationService
from tests.finance_core.test_phase5c_invoice_engine import fetch_one, fetch_scalar, seed_master_data
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6c_checkout_orchestration import FakeRazorpayClient, command, orchestrate
from tests.finance_core.test_phase6d_razorpay_webhook_normalization import (
    PROVIDER_CONFIG,
    razorpay_payload,
    signed_webhook,
)


KEY_SECRET = "phase6aj-fake-key-secret"
WEBHOOK_SECRET = "phase6aj-fake-webhook-secret"


def callback_command(
    *,
    order_id: str = "order_test_1",
    payment_id: str = "pay_phase6aj_1",
    idempotency_key: str = "phase6aj-callback-1",
    signature: str | None = None,
) -> RecordCheckoutCallbackCommand:
    resolved_signature = signature or hmac.digest(
        KEY_SECRET.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        "sha256",
    ).hex()
    return RecordCheckoutCallbackCommand(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=resolved_signature,
        idempotency_key=idempotency_key,
    )


async def seed_checkout(*, idempotency_key: str = "phase6aj-checkout", client=None):
    await seed_master_data()
    return await orchestrate(command(idempotency_key=idempotency_key), client=client)


async def record(command_: RecordCheckoutCallbackCommand):
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutCallbackRecordingService(
            session,
            razorpay_config=sandbox_config(
                key_secret=KEY_SECRET,
                webhook_secret=WEBHOOK_SECRET,
            ),
        )
        result = await service.record_verified_callback(command_)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_verified_callback_binds_payment_records_event_and_authorizes_created_payment():
    checkout, _client = await seed_checkout()

    result = await record(callback_command())

    assert result.payment_id == checkout.finance_checkout_intent_id
    assert result.previous_payment_status == "created"
    assert result.payment_status == "authorized"
    assert result.verification_result == "verified"
    assert result.event_recorded is True
    assert result.replayed is False
    assert result.provider_order_ref.startswith("[REDACTED]...")
    assert result.provider_payment_ref.startswith("[REDACTED]...")

    payment = await fetch_one(
        """
        SELECT status, raw_status, provider_payment_ref, provider_signature_hash
        FROM finance.payments
        WHERE id = :payment_id
        """,
        {"payment_id": checkout.finance_checkout_intent_id},
    )
    assert payment["status"] == "authorized"
    assert payment["raw_status"] == "authorized"
    assert payment["provider_payment_ref"] == "pay_phase6aj_1"
    assert payment["provider_signature_hash"] is None
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events WHERE event_type = 'razorpay.checkout.callback.verified'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 1


@pytest.mark.asyncio
async def test_pending_payment_transitions_to_authorized():
    checkout, _client = await seed_checkout(idempotency_key="phase6aj-pending")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE finance.payments SET status = 'pending', raw_status = 'pending' WHERE id = :payment_id"),
            {"payment_id": checkout.finance_checkout_intent_id},
        )
        await session.commit()

    result = await record(callback_command(idempotency_key="phase6aj-pending-callback"))

    assert result.previous_payment_status == "pending"
    assert result.payment_status == "authorized"


@pytest.mark.asyncio
async def test_invalid_or_missing_signature_fails_before_any_database_mutation():
    checkout, _client = await seed_checkout(idempotency_key="phase6aj-invalid-signature")
    before = await finance_mutation_counts()

    with pytest.raises(RazorpayCheckoutSignatureError):
        await record(callback_command(signature="invalid-signature"))
    with pytest.raises(RazorpayCheckoutSignatureError):
        await record(callback_command(signature=" ", idempotency_key="phase6aj-missing-signature"))

    after = await finance_mutation_counts()
    assert after == before
    assert await fetch_scalar(
        "SELECT provider_payment_ref FROM finance.payments WHERE id = :payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) is None


@pytest.mark.asyncio
async def test_unknown_order_and_wrong_provider_fail_without_mutation():
    await seed_checkout(idempotency_key="phase6aj-unknown")
    with pytest.raises(FinanceCheckoutCallbackError) as unknown:
        await record(
            callback_command(
                order_id="order_missing",
                payment_id="pay_missing",
                idempotency_key="phase6aj-unknown-callback",
            )
        )
    assert unknown.value.code == "CHECKOUT_CALLBACK_ORDER_NOT_FOUND"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE finance.payments SET provider_code = 'other_provider' WHERE provider_order_ref = 'order_test_1'")
        )
        await session.commit()
    with pytest.raises(FinanceCheckoutCallbackError) as wrong_provider:
        await record(callback_command(idempotency_key="phase6aj-wrong-provider"))
    assert wrong_provider.value.code == "CHECKOUT_CALLBACK_ORDER_NOT_FOUND"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0


@pytest.mark.asyncio
async def test_exact_callback_replays_with_same_or_new_idempotency_key_without_duplicates():
    await seed_checkout(idempotency_key="phase6aj-replay")
    first = await record(callback_command(idempotency_key="phase6aj-replay-first"))
    same_key = await record(callback_command(idempotency_key="phase6aj-replay-first"))
    new_key = await record(callback_command(idempotency_key="phase6aj-replay-second"))

    assert first.replayed is False
    assert same_key.replayed is True
    assert same_key.event_recorded is False
    assert new_key.replayed is True
    assert new_key.event_recorded is False
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_callback_conflicts():
    await seed_checkout(idempotency_key="phase6aj-idempotency-conflict")
    await record(callback_command(idempotency_key="phase6aj-same-idem"))

    with pytest.raises(FinancePaymentConflictError):
        await record(
            callback_command(
                payment_id="pay_phase6aj_changed",
                idempotency_key="phase6aj-same-idem",
            )
        )
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 1


@pytest.mark.asyncio
async def test_same_order_different_payment_and_same_payment_different_order_are_rejected():
    await seed_master_data()
    client = FakeRazorpayClient()
    first_checkout, _client = await orchestrate(command(idempotency_key="phase6aj-order-one"), client=client)
    second_checkout, _client = await orchestrate(command(idempotency_key="phase6aj-order-two"), client=client)
    await record(callback_command(order_id="order_test_1", payment_id="pay_shared", idempotency_key="phase6aj-bind-first"))

    with pytest.raises(FinanceCheckoutCallbackError) as different_payment:
        await record(
            callback_command(
                order_id="order_test_1",
                payment_id="pay_different",
                idempotency_key="phase6aj-different-payment",
            )
        )
    assert different_payment.value.code == "CHECKOUT_CALLBACK_PAYMENT_MISMATCH"

    with pytest.raises(FinanceCheckoutCallbackError) as different_order:
        await record(
            callback_command(
                order_id="order_test_2",
                payment_id="pay_shared",
                idempotency_key="phase6aj-different-order",
            )
        )
    assert different_order.value.code == "CHECKOUT_CALLBACK_PAYMENT_CONFLICT"
    assert await fetch_scalar(
        "SELECT provider_payment_ref FROM finance.payments WHERE id = :payment_id",
        {"payment_id": second_checkout.finance_checkout_intent_id},
    ) is None
    assert await fetch_scalar(
        "SELECT provider_payment_ref FROM finance.payments WHERE id = :payment_id",
        {"payment_id": first_checkout.finance_checkout_intent_id},
    ) == "pay_shared"


@pytest.mark.asyncio
@pytest.mark.parametrize("current_status", ["authorized", "captured", "settled"])
async def test_callback_does_not_downgrade_authorized_captured_or_settled_payment(current_status: str):
    checkout, _client = await seed_checkout(idempotency_key=f"phase6aj-{current_status}")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = :payment_status,
                    raw_status = :raw_status,
                    provider_payment_ref = 'pay_phase6aj_1'
                WHERE id = :payment_id
                """
            ),
            {
                "payment_status": current_status,
                "raw_status": current_status,
                "payment_id": checkout.finance_checkout_intent_id,
            },
        )
        await session.commit()

    result = await record(callback_command(idempotency_key=f"phase6aj-{current_status}-callback"))

    assert result.payment_status == current_status
    assert result.replayed is True
    assert result.event_recorded is True
    assert await fetch_scalar(
        "SELECT status FROM finance.payments WHERE id = :payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) == current_status
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0


@pytest.mark.asyncio
async def test_partially_refunded_callback_records_one_stale_trace_without_downgrade_or_side_effects():
    checkout, _client = await seed_checkout(idempotency_key="phase6aj-partially-refunded")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = 'partially_refunded',
                    raw_status = 'partially_refunded',
                    provider_payment_ref = 'pay_phase6aj_partial_refund'
                WHERE id = :payment_id
                """
            ),
            {"payment_id": checkout.finance_checkout_intent_id},
        )
        await session.commit()

    callback = callback_command(
        payment_id="pay_phase6aj_partial_refund",
        idempotency_key="phase6aj-partially-refunded-callback",
    )
    first = await record(callback)
    same_key_replay = await record(callback)
    other_key_replay = await record(
        callback_command(
            payment_id="pay_phase6aj_partial_refund",
            idempotency_key="phase6aj-partially-refunded-callback-replay",
        )
    )

    assert first.verification_result == "verified"
    assert first.previous_payment_status == "partially_refunded"
    assert first.payment_status == "partially_refunded"
    assert first.event_recorded is True
    assert first.replayed is True
    assert same_key_replay.event_recorded is False
    assert same_key_replay.replayed is True
    assert other_key_replay.event_recorded is False
    assert other_key_replay.replayed is True

    payment = await fetch_one(
        """
        SELECT status, provider_payment_ref, provider_signature_hash
        FROM finance.payments
        WHERE id = :payment_id
        """,
        {"payment_id": checkout.finance_checkout_intent_id},
    )
    assert payment["status"] == "partially_refunded"
    assert payment["provider_payment_ref"] == "pay_phase6aj_partial_refund"
    assert payment["provider_signature_hash"] is None
    assert await fetch_scalar(
        "SELECT status FROM finance.invoices WHERE id = :invoice_id",
        {"invoice_id": checkout.finance_invoice_id},
    ) == "issued"
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.payment_events WHERE event_type = 'razorpay.checkout.callback.verified'"
    ) == 1
    assert await fetch_scalar(
        "SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed'"
    ) == 0
    assert callback.razorpay_signature not in repr(first)
    assert KEY_SECRET not in repr(first)
    assert WEBHOOK_SECRET not in repr(first)
    await assert_no_accounting_or_product_side_effects()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "refunded"])
async def test_terminal_payment_state_rejects_callback_without_binding_or_event(terminal_status: str):
    checkout, _client = await seed_checkout(idempotency_key=f"phase6aj-terminal-{terminal_status}")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.payments
                SET status = :payment_status, raw_status = :raw_status
                WHERE id = :payment_id
                """
            ),
            {
                "payment_status": terminal_status,
                "raw_status": terminal_status,
                "payment_id": checkout.finance_checkout_intent_id,
            },
        )
        await session.commit()

    with pytest.raises(FinanceCheckoutCallbackError) as exc:
        await record(callback_command(idempotency_key=f"phase6aj-terminal-callback-{terminal_status}"))
    assert exc.value.code == "CHECKOUT_CALLBACK_PAYMENT_STATE_INVALID"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_events") == 0
    assert await fetch_scalar(
        "SELECT provider_payment_ref FROM finance.payments WHERE id = :payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) is None


@pytest.mark.asyncio
async def test_webhook_can_capture_after_callback_without_callback_accounting_side_effects():
    checkout, _client = await seed_checkout(idempotency_key="phase6aj-webhook-after")
    await record(callback_command(payment_id="pay_phase6aj_webhook", idempotency_key="phase6aj-before-webhook"))

    raw_body = razorpay_payload(
        event_id="evt_phase6aj_captured",
        event_type="payment.captured",
        payment_id="pay_phase6aj_webhook",
        order_id="order_test_1",
        status="captured",
    )
    async with AsyncSessionLocal() as session:
        service = RazorpayWebhookConfirmationService(
            session,
            razorpay_config=sandbox_config(webhook_secret="rzp_webhook_secret"),
            provider_config=PROVIDER_CONFIG,
        )
        webhook_result = await service.confirm_payment_event(
            signed_webhook(raw_body, idempotency_key="phase6aj-webhook-captured")
        )
        await session.commit()

    assert webhook_result.payment_status == "captured"
    assert await fetch_scalar(
        "SELECT status FROM finance.payments WHERE id = :payment_id",
        {"payment_id": checkout.finance_checkout_intent_id},
    ) == "captured"
    await assert_no_accounting_or_product_side_effects()


@pytest.mark.asyncio
async def test_secrets_signature_and_raw_callback_are_not_exposed_or_persisted():
    await seed_checkout(idempotency_key="phase6aj-redaction")
    command_ = callback_command(idempotency_key="phase6aj-redaction-callback")
    result = await record(command_)

    rendered = "\n".join((repr(result), str(result)))
    assert KEY_SECRET not in rendered
    assert WEBHOOK_SECRET not in rendered
    assert command_.razorpay_signature not in rendered
    assert "Authorization" not in rendered
    assert "pay_phase6aj_1" not in rendered
    assert "order_test_1" not in rendered

    stored = await fetch_one(
        """
        SELECT
            provider_signature_hash,
            (SELECT string_agg(event_type || ':' || event_payload_sha256, ',') FROM finance.payment_events) AS event_data
        FROM finance.payments
        LIMIT 1
        """
    )
    assert stored["provider_signature_hash"] is None
    assert command_.razorpay_signature not in (stored["event_data"] or "")
    assert KEY_SECRET not in (stored["event_data"] or "")
    assert WEBHOOK_SECRET not in (stored["event_data"] or "")

    with pytest.raises(RazorpayCheckoutSignatureError) as exc:
        await record(callback_command(signature="fake-secret-looking-invalid-signature", idempotency_key="phase6aj-redaction-error"))
    error_text = str(exc.value)
    assert KEY_SECRET not in error_text
    assert WEBHOOK_SECRET not in error_text
    assert "fake-secret-looking-invalid-signature" not in error_text


@pytest.mark.asyncio
async def test_callback_has_no_allocation_ledger_invoice_paid_or_product_side_effects():
    await seed_checkout(idempotency_key="phase6aj-no-side-effects")
    await record(callback_command(idempotency_key="phase6aj-no-side-effects-callback"))
    await assert_no_accounting_or_product_side_effects()


async def finance_mutation_counts():
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payment_events) AS payment_events,
            (SELECT count(*) FROM finance.idempotency_keys WHERE scope = 'finance.checkout_callback.record') AS callback_keys,
            (SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.state_changed') AS state_events,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.invoices WHERE status = 'paid') AS paid_invoices
        """
    )


async def assert_no_accounting_or_product_side_effects():
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


def test_phase6aj_adds_no_route_frontend_network_or_production_enablement():
    repo_root = Path(__file__).resolve().parents[2]
    service_path = repo_root / "app" / "finance_core" / "services" / "checkout_callbacks.py"
    service_source = service_path.read_text(encoding="utf-8").lower()
    route_source = (repo_root / "app" / "finance_core" / "api" / "payment_boundary.py").read_text(encoding="utf-8")

    assert "apirouter" not in service_source
    assert "requests" not in service_source
    assert "httpx" not in service_source
    assert "aiohttp" not in service_source
    assert "urllib" not in service_source
    assert "http.client" not in service_source
    assert "razorpaycheckoutcallback" not in route_source.lower()
    assert "activate_subscription" not in service_source
    assert "entitlement" not in service_source
    assert not (repo_root / "frontend").exists()
