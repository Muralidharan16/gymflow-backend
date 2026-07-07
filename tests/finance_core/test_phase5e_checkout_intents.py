from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.provider_boundary import (
    CreateCheckoutIntentCommand,
    FinanceCheckoutIntentConflictError,
    FinanceCheckoutIntentStateError,
)
from app.finance_core.services.checkout_intents import FinanceCheckoutIntentService
from tests.finance_core.test_phase5c_invoice_engine import create_draft, draft_command, fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import (
    allocate_payment,
    issued_invoice,
    payment_command,
    record_payment,
    seed_finance_foundation,
)


def checkout_command(
    *,
    invoice_id,
    amount: str = "1180.00",
    idempotency_key: str = "checkout-intent-key-1",
) -> CreateCheckoutIntentCommand:
    return CreateCheckoutIntentCommand(
        organization_id=None,
        invoice_id=invoice_id,
        provider_code="deferred_provider",
        amount=Decimal(amount),
        currency_code="INR",
        idempotency_key=idempotency_key,
    )


async def create_checkout_intent(command: CreateCheckoutIntentCommand):
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        result = await service.create_checkout_intent(command)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_create_checkout_intent_for_issued_invoice_without_payment_capture_or_allocation():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-issued")
    intent = await create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id))

    assert intent.status == "created"
    assert intent.amount == Decimal("1180.00")
    assert intent.currency_code == "INR"
    assert intent.provider_order_ref is not None
    assert await fetch_scalar("SELECT count(*) FROM finance.payments WHERE status = 'captured'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.checkout_intent.created'") == 1


@pytest.mark.asyncio
async def test_draft_invoice_cannot_create_checkout_intent():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="checkout-draft"))
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        with pytest.raises(FinanceCheckoutIntentStateError):
            await service.create_checkout_intent(checkout_command(invoice_id=draft.invoice_id, idempotency_key="checkout-draft-blocked"))
        await session.rollback()


@pytest.mark.asyncio
async def test_paid_invoice_cannot_create_checkout_intent():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-paid")
    payment = await record_payment(payment_command(provider_payment_ref="pay_checkout_paid", idempotency_key="pay-checkout-paid"))
    await allocate_payment(payment.payment_id, invoice.invoice_id, idempotency_key="alloc-checkout-paid")

    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        with pytest.raises(FinanceCheckoutIntentStateError):
            await service.create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id, idempotency_key="checkout-paid-blocked"))
        await session.rollback()


@pytest.mark.asyncio
async def test_checkout_intent_idempotent_replay_and_changed_payload_conflict():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-idem")
    first = await create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id, idempotency_key="checkout-idem-key"))
    replay = await create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id, idempotency_key="checkout-idem-key"))
    assert replay.intent_id == first.intent_id
    assert replay.replayed is True

    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        with pytest.raises(FinanceCheckoutIntentConflictError):
            await service.create_checkout_intent(
                checkout_command(
                    invoice_id=invoice.invoice_id,
                    amount="100.00",
                    idempotency_key="checkout-idem-key",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_checkout_intent_amount_and_currency_must_match_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-amount")
    async with AsyncSessionLocal() as session:
        service = FinanceCheckoutIntentService(session)
        with pytest.raises(FinanceCheckoutIntentStateError):
            await service.create_checkout_intent(
                checkout_command(invoice_id=invoice.invoice_id, amount="1179.99", idempotency_key="checkout-wrong-amount")
            )
        with pytest.raises(FinanceCheckoutIntentStateError):
            await service.create_checkout_intent(
                CreateCheckoutIntentCommand(
                    organization_id=None,
                    invoice_id=invoice.invoice_id,
                    provider_code="deferred_provider",
                    amount=Decimal("1180.00"),
                    currency_code="USD",
                    idempotency_key="checkout-wrong-currency",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_checkout_intent_does_not_change_invoice_payment_state_or_allocate():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-no-side-effect")
    await create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id, idempotency_key="checkout-no-side-effect-key"))

    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "issued"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE entry_type = 'payment'") == 0


@pytest.mark.asyncio
async def test_checkout_intent_rows_do_not_store_provider_secrets_or_raw_tokens():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="checkout-secret-scan")
    intent = await create_checkout_intent(checkout_command(invoice_id=invoice.invoice_id, idempotency_key="checkout-secret-scan-key"))
    row = await fetch_scalar(
        """
        SELECT concat_ws('|', provider_payment_ref, provider_order_ref, provider_signature_hash, raw_status)
        FROM finance.payments
        WHERE id = :intent_id
        """,
        {"intent_id": intent.intent_id},
    )
    lowered = row.lower()
    assert "secret" not in lowered
    assert "token" not in lowered
    assert "key" not in lowered


def test_phase5e_has_no_live_provider_api_or_subscription_activation_behavior():
    finance_root = Path(__file__).resolve().parents[2] / "app" / "finance_core"
    finance_files = [
        *(finance_root / "domain").rglob("*.py"),
        *(finance_root / "repositories").rglob("*.py"),
        *(finance_root / "services").rglob("*.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_files)
    assert "razorpay" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "provider_secret" not in combined
    assert "webhook" not in combined
    assert "activate_subscription" not in combined
    assert "platform_subscriptions" not in combined
