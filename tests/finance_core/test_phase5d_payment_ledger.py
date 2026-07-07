from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.invoice_engine import (
    FinanceInvoiceStateError,
    InvoiceLineInput,
)
from app.finance_core.domain.payment_ledger import (
    AllocatePaymentCommand,
    FinanceLedgerValidationError,
    FinancePaymentConflictError,
    FinancePaymentStateError,
    LedgerLineInput,
    PostLedgerEntryCommand,
    RecordPaymentCommand,
    RecordPaymentEventCommand,
)
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from tests.finance_core.test_phase5c_invoice_engine import (
    BRAND_ID,
    DIVISION_ID,
    GST_REGISTRATION_ID,
    LEGAL_ENTITY_ID,
    create_draft,
    draft_command,
    fetch_one,
    fetch_scalar,
    issue_invoice,
    line,
    seed_master_data,
)


LEDGER_ACCOUNTS = {
    "AR": ("Accounts Receivable", "asset"),
    "PAYMENT_CLEARING": ("Payment Clearing", "asset"),
    "BANK": ("Bank", "asset"),
    "SAAS_REVENUE": ("SaaS Revenue", "revenue"),
    "CGST_PAYABLE": ("CGST Payable", "liability"),
    "SGST_PAYABLE": ("SGST Payable", "liability"),
    "IGST_PAYABLE": ("IGST Payable", "liability"),
    "PG_FEES": ("Payment Gateway Fees", "expense"),
    "REFUNDS_RETURNS": ("Refunds / Sales Returns", "expense"),
    "ROUNDING_ADJUSTMENT": ("Rounding Adjustment", "expense"),
}


async def seed_finance_foundation(*, buyer_state_code: str = "33") -> None:
    await seed_master_data(buyer_state_code=buyer_state_code)
    async with AsyncSessionLocal() as session:
        for code, (name, account_type) in LEDGER_ACCOUNTS.items():
            await session.execute(
                text(
                    """
                    INSERT INTO finance.ledger_accounts (
                        legal_entity_id, code, name, account_type, status
                    )
                    VALUES (:legal_entity_id, :code, :name, :account_type, 'active')
                    """
                ),
                {
                    "legal_entity_id": LEGAL_ENTITY_ID,
                    "code": code,
                    "name": name,
                    "account_type": account_type,
                },
            )
        await session.commit()


def payment_command(
    *,
    provider_payment_ref: str = "pay_test_1",
    amount: str = "1180.00",
    status: str = "captured",
    idempotency_key: str = "payment-key-1",
) -> RecordPaymentCommand:
    return RecordPaymentCommand(
        organization_id=None,
        legal_entity_id=LEGAL_ENTITY_ID,
        gst_registration_id=GST_REGISTRATION_ID,
        division_id=DIVISION_ID,
        brand_id=BRAND_ID,
        provider_code="test_provider",
        provider_payment_ref=provider_payment_ref,
        provider_order_ref="order_test_1",
        provider_signature_hash=None,
        amount=Decimal(amount),
        currency_code="INR",
        status=status,
        raw_status=status,
        idempotency_key=idempotency_key,
    )


async def record_payment(command: RecordPaymentCommand | None = None):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.record_payment(command or payment_command())
        await session.commit()
        return result


async def allocate_payment(payment_id: uuid.UUID, invoice_id: uuid.UUID, *, amount: str = "1180.00", idempotency_key: str = "alloc-key-1"):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.allocate_payment_to_invoice(
            AllocatePaymentCommand(
                payment_id=payment_id,
                invoice_id=invoice_id,
                amount=Decimal(amount),
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


async def issued_invoice(*, amount: str = "1000.00", idempotency_key: str = "draft-for-payment"):
    draft = await create_draft(
        draft_command(
            idempotency_key=idempotency_key,
            line_items=(line(unit_price=amount),),
        )
    )
    return await issue_invoice(draft.invoice_id, idempotency_key=f"{idempotency_key}-issue")


async def ledger_balance(ledger_entry_id: uuid.UUID):
    return await fetch_one(
        """
        SELECT
            coalesce(sum(debit_amount), 0) AS debits,
            coalesce(sum(credit_amount), 0) AS credits
        FROM finance.ledger_entry_lines
        WHERE ledger_entry_id = :ledger_entry_id
        """,
        {"ledger_entry_id": ledger_entry_id},
    )


@pytest.mark.asyncio
async def test_record_captured_pending_and_failed_payments_without_allocation_side_effects():
    await seed_finance_foundation()
    captured = await record_payment(payment_command(status="captured", provider_payment_ref="pay_captured", idempotency_key="pay-captured"))
    pending = await record_payment(payment_command(status="pending", provider_payment_ref="pay_pending", idempotency_key="pay-pending"))
    failed = await record_payment(payment_command(status="failed", provider_payment_ref="pay_failed", idempotency_key="pay-failed"))

    assert captured.status == "captured"
    assert pending.status == "pending"
    assert failed.status == "failed"
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.recorded'") == 3


@pytest.mark.asyncio
async def test_duplicate_provider_payment_ref_is_rejected_and_idempotency_replays():
    await seed_finance_foundation()
    first = await record_payment(payment_command(provider_payment_ref="pay_duplicate", idempotency_key="pay-duplicate-1"))
    replay = await record_payment(payment_command(provider_payment_ref="pay_duplicate", idempotency_key="pay-duplicate-1"))
    assert replay.payment_id == first.payment_id
    assert replay.replayed is True

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.record_payment(
                payment_command(provider_payment_ref="pay_duplicate", idempotency_key="pay-duplicate-2")
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payment_payload_conflicts():
    await seed_finance_foundation()
    await record_payment(payment_command(provider_payment_ref="pay_idem", amount="1180.00", idempotency_key="pay-idem"))
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.record_payment(
                payment_command(provider_payment_ref="pay_idem_changed", amount="1000.00", idempotency_key="pay-idem")
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_record_payment_event_is_idempotent_and_duplicate_provider_event_conflicts():
    await seed_finance_foundation()
    payment = await record_payment(payment_command(provider_payment_ref="pay_event", idempotency_key="pay-event-payment"))
    command = RecordPaymentEventCommand(
        payment_id=payment.payment_id,
        provider_code="test_provider",
        provider_event_id="evt_test_1",
        event_type="payment.captured",
        event_payload_sha256="a" * 64,
        idempotency_key="event-key-1",
    )
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        first = await service.record_payment_event(command)
        replay = await service.record_payment_event(command)
        assert replay.payment_event_id == first.payment_event_id
        assert replay.replayed is True
        with pytest.raises(FinancePaymentConflictError):
            await service.record_payment_event(
                RecordPaymentEventCommand(
                    payment_id=payment.payment_id,
                    provider_code="test_provider",
                    provider_event_id="evt_test_1",
                    event_type="payment.captured",
                    event_payload_sha256="a" * 64,
                    idempotency_key="event-key-2",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_captured_payment_allocation_to_issued_invoice_marks_paid_and_posts_ledger_and_outbox():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    payment = await record_payment(payment_command(provider_payment_ref="pay_alloc_full", idempotency_key="pay-alloc-full"))
    allocation = await allocate_payment(payment.payment_id, invoice.invoice_id)

    assert allocation.invoice_status == "paid"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.allocated'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.invoice.paid'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.ledger.entry.posted'") == 1


@pytest.mark.asyncio
async def test_partial_payment_marks_invoice_partially_paid():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    payment = await record_payment(payment_command(provider_payment_ref="pay_partial", amount="500.00", idempotency_key="pay-partial"))
    allocation = await allocate_payment(payment.payment_id, invoice.invoice_id, amount="500.00", idempotency_key="alloc-partial")
    assert allocation.invoice_status == "partially_paid"
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "partially_paid"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "failed", "cancelled"])
async def test_pending_failed_or_cancelled_payment_cannot_be_allocated(status: str):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=f"draft-blocked-{status}")
    payment = await record_payment(
        payment_command(provider_payment_ref=f"pay_{status}", status=status, idempotency_key=f"pay-{status}")
    )
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.allocate_payment_to_invoice(
                AllocatePaymentCommand(
                    payment_id=payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("100.00"),
                    idempotency_key=f"alloc-{status}",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_allocation_cannot_exceed_payment_balance_or_invoice_outstanding_and_paid_invoice_cannot_be_overpaid():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    small_payment = await record_payment(payment_command(provider_payment_ref="pay_small", amount="100.00", idempotency_key="pay-small"))
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.allocate_payment_to_invoice(
                AllocatePaymentCommand(
                    payment_id=small_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("101.00"),
                    idempotency_key="alloc-too-much-payment",
                )
            )
        await session.rollback()

    large_payment = await record_payment(payment_command(provider_payment_ref="pay_large", amount="2000.00", idempotency_key="pay-large"))
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.allocate_payment_to_invoice(
                AllocatePaymentCommand(
                    payment_id=large_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("1180.01"),
                    idempotency_key="alloc-too-much-invoice",
                )
            )
        await session.rollback()

    full_payment = await record_payment(payment_command(provider_payment_ref="pay_paid", amount="1180.00", idempotency_key="pay-paid"))
    await allocate_payment(full_payment.payment_id, invoice.invoice_id, idempotency_key="alloc-paid")
    second_payment = await record_payment(payment_command(provider_payment_ref="pay_overpaid", amount="1.00", idempotency_key="pay-overpaid"))
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceInvoiceStateError):
            await service.allocate_payment_to_invoice(
                AllocatePaymentCommand(
                    payment_id=second_payment.payment_id,
                    invoice_id=invoice.invoice_id,
                    amount=Decimal("1.00"),
                    idempotency_key="alloc-overpaid",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_draft_invoice_cannot_receive_final_payment_allocation():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="draft-no-alloc"))
    payment = await record_payment(payment_command(provider_payment_ref="pay_draft", idempotency_key="pay-draft"))
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceInvoiceStateError):
            await service.allocate_payment_to_invoice(
                AllocatePaymentCommand(
                    payment_id=payment.payment_id,
                    invoice_id=draft.invoice_id,
                    amount=Decimal("100.00"),
                    idempotency_key="alloc-draft",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_ledger_entry_must_balance_and_lines_must_be_one_sided():
    await seed_finance_foundation()
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceLedgerValidationError):
            await service.post_ledger_entry(
                PostLedgerEntryCommand(
                    legal_entity_id=LEGAL_ENTITY_ID,
                    division_id=DIVISION_ID,
                    brand_id=BRAND_ID,
                    entry_type="adjustment",
                    source_type="test",
                    source_id=uuid.uuid4(),
                    idempotency_key="ledger-unbalanced",
                    lines=(
                        LedgerLineInput(account_code="AR", debit_amount=Decimal("10.00")),
                        LedgerLineInput(account_code="SAAS_REVENUE", credit_amount=Decimal("9.00")),
                    ),
                )
            )
        with pytest.raises(FinanceLedgerValidationError):
            await service.post_ledger_entry(
                PostLedgerEntryCommand(
                    legal_entity_id=LEGAL_ENTITY_ID,
                    division_id=DIVISION_ID,
                    brand_id=BRAND_ID,
                    entry_type="adjustment",
                    source_type="test",
                    source_id=uuid.uuid4(),
                    idempotency_key="ledger-two-sided",
                    lines=(
                        LedgerLineInput(account_code="AR", debit_amount=Decimal("10.00"), credit_amount=Decimal("1.00")),
                        LedgerLineInput(account_code="SAAS_REVENUE", credit_amount=Decimal("9.00")),
                    ),
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_invoice_issued_ledger_entry_balances():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        entry = await service.post_invoice_issued_entry(invoice_id=invoice.invoice_id, idempotency_key="ledger-invoice-issued")
        await session.commit()

    balance = await ledger_balance(entry.ledger_entry_id)
    assert balance["debits"] == balance["credits"] == Decimal("1180.00")
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entry_lines WHERE ledger_entry_id = :id", {"id": entry.ledger_entry_id}) == 4
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.ledger.entry.posted'") == 1


@pytest.mark.asyncio
async def test_payment_captured_ledger_entry_balances_and_retry_allocation_does_not_duplicate_ledger():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    payment = await record_payment(payment_command(provider_payment_ref="pay_retry_alloc", idempotency_key="pay-retry-alloc"))
    first = await allocate_payment(payment.payment_id, invoice.invoice_id, idempotency_key="alloc-retry")
    replay = await allocate_payment(payment.payment_id, invoice.invoice_id, idempotency_key="alloc-retry")

    assert replay.allocation_id == first.allocation_id
    assert replay.replayed is True
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'payment_allocation'") == 1
    balance = await fetch_one(
        """
        SELECT coalesce(sum(debit_amount), 0) AS debits, coalesce(sum(credit_amount), 0) AS credits
        FROM finance.ledger_entry_lines
        """
    )
    assert balance["debits"] == balance["credits"] == Decimal("1180.00")


@pytest.mark.asyncio
async def test_issued_invoice_remains_immutable_through_invoice_service_after_payment_foundation():
    await seed_finance_foundation()
    invoice = await issued_invoice()
    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        with pytest.raises(FinanceInvoiceStateError):
            await engine.replace_draft_lines(
                invoice_id=invoice.invoice_id,
                line_items=(
                    InvoiceLineInput(
                        description="Changed line",
                        quantity=Decimal("1"),
                        unit_price=Decimal("1.00"),
                        hsn_sac="998313",
                        gst_rate_basis_points=1800,
                        pricing_mode="tax_exclusive",
                    ),
                ),
            )
        await session.rollback()


def test_phase5d_has_no_real_provider_api_or_subscription_activation_behavior():
    finance_root = Path(__file__).resolve().parents[2] / "app" / "finance_core"
    finance_files = [
        *(finance_root / "domain").rglob("*.py"),
        *(finance_root / "repositories").rglob("*.py"),
        *(finance_root / "services").rglob("*.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_files)
    assert "razorpay import" not in combined
    assert "razorpay.client" not in combined
    assert "apirouter" not in combined
    assert "@router" not in combined
    assert "activate_subscription" not in combined
    assert "platform_subscriptions" not in combined
