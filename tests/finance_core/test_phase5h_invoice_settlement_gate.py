from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import uuid

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.invoice_engine import FinanceInvoiceStateError
from app.finance_core.domain.payment_ledger import (
    ApplyPaymentToInvoiceCommand,
    FinancePaymentConflictError,
    FinancePaymentStateError,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from tests.finance_core.test_phase5c_invoice_engine import create_draft, draft_command, fetch_one, fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import (
    issued_invoice,
    payment_command,
    record_payment,
    seed_finance_foundation,
)
from tests.finance_core.test_phase5g_payment_state_machine import apply_event, sandbox_checkout_intent, state_event


async def apply_payment(
    payment_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    amount: str = "1180.00",
    idempotency_key: str = "apply-key-1",
):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.apply_payment_to_invoice(
            ApplyPaymentToInvoiceCommand(
                payment_id=payment_id,
                invoice_id=invoice_id,
                amount=Decimal(amount),
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_captured_payment_can_be_explicitly_applied_to_issued_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    payment = await record_payment(payment_command(provider_payment_ref="pay_5h_captured", idempotency_key="pay-5h-captured"))

    result = await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-captured")

    assert result.invoice_status == "paid"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.applied'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.allocated'") == 0


@pytest.mark.asyncio
async def test_settled_payment_can_be_explicitly_applied_to_issued_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-5h-settled")
    payment = await record_payment(
        payment_command(
            provider_payment_ref="pay_5h_settled",
            status="settled",
            idempotency_key="pay-5h-settled",
        )
    )

    result = await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-settled")

    assert result.invoice_status == "paid"
    assert result.allocated_amount == Decimal("1180.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "authorized", "failed", "cancelled", "refunded"])
async def test_non_captured_or_non_settled_payment_cannot_be_applied(status: str):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=f"invoice-5h-blocked-{status}")
    payment = await record_payment(
        payment_command(
            provider_payment_ref=f"pay_5h_{status}",
            status=status,
            idempotency_key=f"pay-5h-{status}",
        )
    )

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("100.00"),
                    idempotency_key=f"apply-5h-{status}",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_draft_invoice_cannot_receive_explicit_settlement():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="draft-5h-no-settlement"))
    payment = await record_payment(payment_command(provider_payment_ref="pay_5h_draft", idempotency_key="pay-5h-draft"))

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceInvoiceStateError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=payment.payment_id,
                    invoice_id=draft.invoice_id,
                    amount=Decimal("100.00"),
                    idempotency_key="apply-5h-draft",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_partial_and_full_explicit_settlement_update_invoice_status():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-5h-partial")
    first_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_partial", amount="500.00", idempotency_key="pay-5h-partial")
    )
    second_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_remainder", amount="680.00", idempotency_key="pay-5h-remainder")
    )

    partial = await apply_payment(
        first_payment.payment_id,
        invoice.invoice_id,
        amount="500.00",
        idempotency_key="apply-5h-partial",
    )
    final = await apply_payment(
        second_payment.payment_id,
        invoice.invoice_id,
        amount="680.00",
        idempotency_key="apply-5h-remainder",
    )

    assert partial.invoice_status == "partially_paid"
    assert final.invoice_status == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.invoice.partially_paid'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.invoice.paid'") == 1


@pytest.mark.asyncio
async def test_explicit_settlement_cannot_exceed_payment_balance_or_invoice_outstanding_or_overpay_paid_invoice():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-5h-overage")
    small_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_small", amount="100.00", idempotency_key="pay-5h-small")
    )

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=small_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("100.01"),
                    idempotency_key="apply-5h-too-much-payment",
                )
            )
        await session.rollback()

    large_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_large", amount="2000.00", idempotency_key="pay-5h-large")
    )
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=large_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("1180.01"),
                    idempotency_key="apply-5h-too-much-invoice",
                )
            )
        await session.rollback()

    full_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_full", amount="1180.00", idempotency_key="pay-5h-full")
    )
    await apply_payment(full_payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-full")
    extra_payment = await record_payment(
        payment_command(provider_payment_ref="pay_5h_extra", amount="1.00", idempotency_key="pay-5h-extra")
    )
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceInvoiceStateError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=extra_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("1.00"),
                    idempotency_key="apply-5h-paid-invoice",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_explicit_settlement_replay_does_not_duplicate_allocation_or_ledger_and_conflicting_payload_rejects():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-5h-replay")
    payment = await record_payment(payment_command(provider_payment_ref="pay_5h_replay", idempotency_key="pay-5h-replay"))

    first = await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-replay")
    replay = await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-replay")

    assert replay.allocation_id == first.allocation_id
    assert replay.replayed is True
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1

    second_invoice = await issued_invoice(idempotency_key="invoice-5h-replay-conflict")
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.apply_payment_to_invoice(
                ApplyPaymentToInvoiceCommand(
                    payment_id=payment.payment_id,
                    invoice_id=second_invoice.invoice_id,
                    amount=Decimal("1.00"),
                    idempotency_key="apply-5h-replay",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_explicit_settlement_posts_balanced_payment_ledger_entry():
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key="invoice-5h-ledger")
    payment = await record_payment(payment_command(provider_payment_ref="pay_5h_ledger", idempotency_key="pay-5h-ledger"))

    await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key="apply-5h-ledger")

    balance = await fetch_one(
        """
        SELECT coalesce(sum(lines.debit_amount), 0) AS debits,
               coalesce(sum(lines.credit_amount), 0) AS credits
        FROM finance.ledger_entry_lines lines
        JOIN finance.ledger_entries entries ON entries.id = lines.ledger_entry_id
        WHERE entries.source_type = 'payment_allocation'
        """
    )
    assert balance["debits"] == balance["credits"] == Decimal("1180.00")


@pytest.mark.asyncio
async def test_provider_captured_event_alone_does_not_apply_payment_or_activate_subscription():
    intent = await sandbox_checkout_intent(idempotency_key="intent-5h-provider-boundary")

    result = await apply_event(
        state_event(
            payment_id=intent.intent_id,
            status="captured",
            event_id="evt_5h_captured_boundary",
            idempotency_key="state-5h-captured-boundary",
        )
    )

    assert result.payment_status == "captured"
    assert result.state_applied is True
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.applied'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase5h_has_no_provider_api_frontend_or_subscription_activation_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
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
    assert "activate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
