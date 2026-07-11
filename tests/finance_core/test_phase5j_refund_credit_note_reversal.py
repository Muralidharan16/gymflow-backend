from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.invoice_engine import FinanceInvoiceStateError
from app.finance_core.domain.payment_ledger import (
    CreateCreditNoteCommand,
    CreateRefundIntentCommand,
    FinancePaymentConflictError,
    FinancePaymentStateError,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from tests.finance_core.test_phase5c_invoice_engine import create_draft, draft_command, fetch_one, fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import issued_invoice, payment_command, record_payment, seed_finance_foundation
from tests.finance_core.test_phase5h_invoice_settlement_gate import apply_payment


async def paid_invoice_with_payment(
    *,
    invoice_key: str = "invoice-5j-paid",
    payment_key: str = "pay-5j-paid",
    payment_ref: str = "pay_5j_paid",
    payment_status: str = "captured",
):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=invoice_key)
    payment = await record_payment(
        payment_command(
            provider_payment_ref=payment_ref,
            status=payment_status,
            idempotency_key=payment_key,
        )
    )
    await apply_payment(payment.payment_id, invoice.invoice_id, idempotency_key=f"{payment_key}-apply")
    return invoice, payment


async def create_credit_note(
    invoice_id,
    *,
    credit_note_ref: str = "CN-5J-001",
    amount: str = "1180.00",
    reason: str = "customer correction",
    idempotency_key: str = "credit-5j-1",
):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.create_credit_note(
            CreateCreditNoteCommand(
                invoice_id=invoice_id,
                credit_note_ref=credit_note_ref,
                amount=Decimal(amount),
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


async def create_refund_intent(
    payment_id,
    *,
    refund_ref: str = "RF-5J-001",
    amount: str = "1180.00",
    reason: str = "customer correction",
    credit_note_id=None,
    idempotency_key: str = "refund-5j-1",
):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.create_refund_intent(
            CreateRefundIntentCommand(
                payment_id=payment_id,
                refund_ref=refund_ref,
                amount=Decimal(amount),
                reason=reason,
                credit_note_id=credit_note_id,
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


def _invoice_snapshot_sql() -> str:
    return """
    SELECT
        seller_legal_name,
        seller_gstin,
        seller_pan,
        seller_registered_address,
        seller_state_code,
        buyer_billing_name,
        buyer_address,
        buyer_gstin,
        buyer_pan,
        buyer_place_of_supply_state_code,
        buyer_gst_treatment
    FROM finance.invoices
    WHERE id = :id
    """


@pytest.mark.asyncio
async def test_create_credit_note_for_issued_invoice_posts_balanced_reversal_entry():
    invoice, _payment = await paid_invoice_with_payment()

    result = await create_credit_note(invoice.invoice_id)

    assert result.invoice_id == invoice.invoice_id
    assert result.credit_note_ref == "CN-5J-001"
    assert await fetch_scalar("SELECT count(*) FROM finance.credit_notes") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.credit_note_lines") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.credit_note.issued'") == 1
    balance = await fetch_one(
        """
        SELECT coalesce(sum(debit_amount), 0) AS debits,
               coalesce(sum(credit_amount), 0) AS credits
        FROM finance.ledger_entry_lines
        WHERE ledger_entry_id = :ledger_entry_id
        """,
        {"ledger_entry_id": result.ledger_entry_id},
    )
    assert balance["debits"] == balance["credits"] == Decimal("1180.00")


@pytest.mark.asyncio
async def test_draft_invoice_cannot_receive_credit_note():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="draft-5j-credit"))

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinanceInvoiceStateError):
            await service.create_credit_note(
                CreateCreditNoteCommand(
                    invoice_id=draft.invoice_id,
                    credit_note_ref="CN-5J-DRAFT",
                    amount=Decimal("1.00"),
                    reason="draft correction",
                    idempotency_key="credit-5j-draft",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_credit_note_cannot_exceed_eligible_amount_and_does_not_mutate_invoice_lines_or_snapshots():
    invoice, _payment = await paid_invoice_with_payment(invoice_key="invoice-5j-eligible", payment_key="pay-5j-eligible", payment_ref="pay_5j_eligible")
    before_line_count = await fetch_scalar("SELECT count(*) FROM finance.invoice_lines WHERE invoice_id = :id", {"id": invoice.invoice_id})
    before_snapshot = await fetch_one(_invoice_snapshot_sql(), {"id": invoice.invoice_id})

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.create_credit_note(
                CreateCreditNoteCommand(
                    invoice_id=invoice.invoice_id,
                    credit_note_ref="CN-5J-TOO-MUCH",
                    amount=Decimal("1180.01"),
                    reason="too much",
                    idempotency_key="credit-5j-too-much",
                )
            )
        await session.rollback()

    await create_credit_note(invoice.invoice_id, credit_note_ref="CN-5J-PARTIAL", amount="500.00", idempotency_key="credit-5j-partial")
    assert await fetch_scalar("SELECT count(*) FROM finance.invoice_lines WHERE invoice_id = :id", {"id": invoice.invoice_id}) == before_line_count
    after_snapshot = await fetch_one(_invoice_snapshot_sql(), {"id": invoice.invoice_id})
    assert after_snapshot == before_snapshot


@pytest.mark.asyncio
async def test_credit_note_idempotency_replay_and_conflict_do_not_duplicate_ledger():
    invoice, _payment = await paid_invoice_with_payment(invoice_key="invoice-5j-idem", payment_key="pay-5j-idem", payment_ref="pay_5j_idem")
    first = await create_credit_note(invoice.invoice_id, credit_note_ref="CN-5J-IDEM", idempotency_key="credit-5j-idem")
    replay = await create_credit_note(invoice.invoice_id, credit_note_ref="CN-5J-IDEM", idempotency_key="credit-5j-idem")

    assert replay.credit_note_id == first.credit_note_id
    assert replay.ledger_entry_id == first.ledger_entry_id
    assert replay.replayed is True
    assert await fetch_scalar("SELECT count(*) FROM finance.credit_notes") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'credit_note'") == 1

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.create_credit_note(
                CreateCreditNoteCommand(
                    invoice_id=invoice.invoice_id,
                    credit_note_ref="CN-5J-IDEM-CHANGED",
                    amount=Decimal("1.00"),
                    reason="changed",
                    idempotency_key="credit-5j-idem",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_create_refund_intent_for_eligible_payment_without_provider_execution():
    invoice, payment = await paid_invoice_with_payment(invoice_key="invoice-5j-refund", payment_key="pay-5j-refund", payment_ref="pay_5j_refund")
    credit_note = await create_credit_note(invoice.invoice_id, credit_note_ref="CN-5J-REFUND", idempotency_key="credit-5j-refund")

    result = await create_refund_intent(
        payment.payment_id,
        refund_ref="RF-5J-REFUND",
        credit_note_id=credit_note.credit_note_id,
        idempotency_key="refund-5j-refund",
    )

    assert result.status == "requested"
    assert await fetch_scalar("SELECT count(*) FROM finance.refunds") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.refund.intent.created'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'refund'") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "authorized", "failed", "cancelled", "refunded"])
async def test_unqualified_payment_cannot_receive_refund_intent(status: str):
    await seed_finance_foundation()
    payment = await record_payment(
        payment_command(
            provider_payment_ref=f"pay_5j_{status}",
            status=status,
            idempotency_key=f"pay-5j-{status}",
        )
    )

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.create_refund_intent(
                CreateRefundIntentCommand(
                    payment_id=payment.payment_id,
                    refund_ref=f"RF-5J-{status}",
                    amount=Decimal("1.00"),
                    reason="blocked refund",
                    credit_note_id=None,
                    idempotency_key=f"refund-5j-{status}",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_refund_intent_preserves_allocation_history_and_subscription_state():
    _invoice, payment = await paid_invoice_with_payment(invoice_key="invoice-5j-side-effects", payment_key="pay-5j-side-effects", payment_ref="pay_5j_side_effects")
    before_allocations = await fetch_scalar("SELECT count(*) FROM finance.payment_allocations")

    await create_refund_intent(payment.payment_id, refund_ref="RF-5J-SIDE", amount="100.00", idempotency_key="refund-5j-side")

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == before_allocations
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'refund'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


@pytest.mark.asyncio
async def test_refund_ref_duplicate_and_idempotency_conflict_are_enforced():
    _invoice, payment = await paid_invoice_with_payment(invoice_key="invoice-5j-dup", payment_key="pay-5j-dup", payment_ref="pay_5j_dup")
    first = await create_refund_intent(payment.payment_id, refund_ref="RF-5J-DUP", amount="100.00", idempotency_key="refund-5j-dup")
    replay = await create_refund_intent(payment.payment_id, refund_ref="RF-5J-DUP", amount="100.00", idempotency_key="refund-5j-dup")

    assert replay.refund_id == first.refund_id
    assert replay.replayed is True

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.create_refund_intent(
                CreateRefundIntentCommand(
                    payment_id=payment.payment_id,
                    refund_ref="RF-5J-DUP",
                    amount=Decimal("100.00"),
                    reason="duplicate ref",
                    credit_note_id=None,
                    idempotency_key="refund-5j-dup-other",
                )
            )
        with pytest.raises(FinancePaymentConflictError):
            await service.create_refund_intent(
                CreateRefundIntentCommand(
                    payment_id=payment.payment_id,
                    refund_ref="RF-5J-CHANGED",
                    amount=Decimal("1.00"),
                    reason="changed",
                    credit_note_id=None,
                    idempotency_key="refund-5j-dup",
                )
            )
        await session.rollback()


def test_phase5j_has_no_live_provider_frontend_or_production_enablement():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    finance_files = [
        *(finance_root / "domain").rglob("*.py"),
        *(finance_root / "repositories").rglob("*.py"),
        *(finance_root / "services").rglob("*.py"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_files)
    assert "rzp_live_" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert not (repo_root / "frontend").exists()
