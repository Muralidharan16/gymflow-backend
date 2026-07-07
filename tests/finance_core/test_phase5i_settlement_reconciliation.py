from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    FinancePaymentStateError,
    ReconcilePaymentSettlementCommand,
)
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService
from tests.finance_core.test_phase5c_invoice_engine import fetch_one, fetch_scalar
from tests.finance_core.test_phase5d_payment_ledger import issued_invoice, payment_command, record_payment, seed_finance_foundation
from tests.finance_core.test_phase5h_invoice_settlement_gate import apply_payment


async def applied_payment(
    *,
    status: str = "captured",
    amount: str = "1180.00",
    payment_key: str = "pay-5i-applied",
    payment_ref: str = "pay_5i_applied",
    invoice_key: str = "invoice-5i-applied",
):
    await seed_finance_foundation()
    invoice = await issued_invoice(idempotency_key=invoice_key)
    payment = await record_payment(
        payment_command(
            provider_payment_ref=payment_ref,
            amount=amount,
            status=status,
            idempotency_key=payment_key,
        )
    )
    await apply_payment(payment.payment_id, invoice.invoice_id, amount=amount, idempotency_key=f"{payment_key}-apply")
    return invoice, payment


async def reconcile_payment(
    payment_id,
    *,
    settlement_ref: str = "settlement-5i-1",
    settlement_amount: str = "1180.00",
    gateway_fee_amount: str = "0.00",
    idempotency_key: str = "settle-5i-1",
):
    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        result = await service.reconcile_payment_settlement(
            ReconcilePaymentSettlementCommand(
                payment_id=payment_id,
                settlement_ref=settlement_ref,
                settlement_amount=Decimal(settlement_amount),
                gateway_fee_amount=Decimal(gateway_fee_amount),
                idempotency_key=idempotency_key,
            )
        )
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_reconcile_applied_captured_payment_to_bank():
    invoice, payment = await applied_payment()

    result = await reconcile_payment(payment.payment_id)

    assert result.payment_id == payment.payment_id
    assert result.settlement_ref == "settlement-5i-1"
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries WHERE source_type = 'settlement'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.payment.reconciled'") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE event_type = 'finance.settlement.reconciled'") == 1
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == "paid"


@pytest.mark.asyncio
async def test_reconcile_applied_settled_payment_to_bank():
    _invoice, payment = await applied_payment(
        status="settled",
        payment_key="pay-5i-settled",
        payment_ref="pay_5i_settled",
        invoice_key="invoice-5i-settled",
    )

    result = await reconcile_payment(payment.payment_id, settlement_ref="settlement-5i-settled", idempotency_key="settle-5i-settled")

    assert result.settlement_amount == Decimal("1180.00")
    assert result.gateway_fee_amount == Decimal("0.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "authorized", "failed", "cancelled", "refunded"])
async def test_unqualified_payment_status_cannot_be_reconciled(status: str):
    await seed_finance_foundation()
    payment = await record_payment(
        payment_command(
            provider_payment_ref=f"pay_5i_{status}",
            status=status,
            idempotency_key=f"pay-5i-{status}",
        )
    )

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref=f"settlement-5i-{status}",
                    settlement_amount=Decimal("100.00"),
                    idempotency_key=f"settle-5i-{status}",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_captured_but_unapplied_payment_cannot_be_reconciled():
    await seed_finance_foundation()
    payment = await record_payment(payment_command(provider_payment_ref="pay_5i_unapplied", idempotency_key="pay-5i-unapplied"))

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref="settlement-5i-unapplied",
                    settlement_amount=Decimal("100.00"),
                    idempotency_key="settle-5i-unapplied",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_duplicate_settlement_ref_is_rejected_unless_idempotent_replay():
    _invoice, payment = await applied_payment(payment_key="pay-5i-duplicate", payment_ref="pay_5i_duplicate", invoice_key="invoice-5i-duplicate")
    first = await reconcile_payment(payment.payment_id, settlement_ref="settlement-5i-duplicate", idempotency_key="settle-5i-duplicate")
    replay = await reconcile_payment(payment.payment_id, settlement_ref="settlement-5i-duplicate", idempotency_key="settle-5i-duplicate")

    assert replay.ledger_entry_id == first.ledger_entry_id
    assert replay.replayed is True

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref="settlement-5i-duplicate",
                    settlement_amount=Decimal("1.00"),
                    idempotency_key="settle-5i-duplicate-other",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payload_conflicts():
    _invoice, payment = await applied_payment(payment_key="pay-5i-idem", payment_ref="pay_5i_idem", invoice_key="invoice-5i-idem")
    await reconcile_payment(payment.payment_id, settlement_ref="settlement-5i-idem", idempotency_key="settle-5i-idem")

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentConflictError):
            await service.reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref="settlement-5i-idem-changed",
                    settlement_amount=Decimal("1.00"),
                    idempotency_key="settle-5i-idem",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_settlement_and_gateway_fee_ledger_entries_balance():
    _invoice, payment = await applied_payment(payment_key="pay-5i-fee", payment_ref="pay_5i_fee", invoice_key="invoice-5i-fee")

    result = await reconcile_payment(
        payment.payment_id,
        settlement_ref="settlement-5i-fee",
        settlement_amount="1180.00",
        gateway_fee_amount="30.00",
        idempotency_key="settle-5i-fee",
    )

    balance = await fetch_one(
        """
        SELECT coalesce(sum(lines.debit_amount), 0) AS debits,
               coalesce(sum(lines.credit_amount), 0) AS credits
        FROM finance.ledger_entry_lines lines
        WHERE lines.ledger_entry_id = :ledger_entry_id
        """,
        {"ledger_entry_id": result.ledger_entry_id},
    )
    assert balance["debits"] == balance["credits"] == Decimal("1180.00")
    assert await fetch_scalar(
        """
        SELECT count(*)
        FROM finance.ledger_entry_lines lines
        JOIN finance.ledger_accounts accounts ON accounts.id = lines.ledger_account_id
        WHERE lines.ledger_entry_id = :ledger_entry_id
          AND accounts.code = 'PG_FEES'
          AND lines.debit_amount = 30.00
        """,
        {"ledger_entry_id": result.ledger_entry_id},
    ) == 1


@pytest.mark.asyncio
async def test_negative_or_excessive_gateway_fee_is_rejected():
    _invoice, payment = await applied_payment(payment_key="pay-5i-bad-fee", payment_ref="pay_5i_bad_fee", invoice_key="invoice-5i-bad-fee")

    for fee, key in [("-1.00", "settle-5i-negative-fee"), ("1180.01", "settle-5i-excessive-fee")]:
        async with AsyncSessionLocal() as session:
            service = FinancePaymentLedgerService(session)
            with pytest.raises(FinancePaymentStateError):
                await service.reconcile_payment_settlement(
                    ReconcilePaymentSettlementCommand(
                        payment_id=payment.payment_id,
                        settlement_ref=key,
                        settlement_amount=Decimal("1180.00"),
                        gateway_fee_amount=Decimal(fee),
                        idempotency_key=key,
                    )
                )
            await session.rollback()


@pytest.mark.asyncio
async def test_settlement_amount_above_unreconciled_clearing_amount_is_rejected():
    _invoice, payment = await applied_payment(payment_key="pay-5i-over", payment_ref="pay_5i_over", invoice_key="invoice-5i-over")

    async with AsyncSessionLocal() as session:
        service = FinancePaymentLedgerService(session)
        with pytest.raises(FinancePaymentStateError):
            await service.reconcile_payment_settlement(
                ReconcilePaymentSettlementCommand(
                    payment_id=payment.payment_id,
                    settlement_ref="settlement-5i-over",
                    settlement_amount=Decimal("1180.01"),
                    idempotency_key="settle-5i-over",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_reconciliation_does_not_allocate_mark_invoice_paid_or_activate_subscription():
    invoice, payment = await applied_payment(
        amount="500.00",
        payment_key="pay-5i-side-effects",
        payment_ref="pay_5i_side_effects",
        invoice_key="invoice-5i-side-effects",
    )
    before_allocations = await fetch_scalar("SELECT count(*) FROM finance.payment_allocations")
    before_invoice_status = await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id})

    await reconcile_payment(
        payment.payment_id,
        settlement_ref="settlement-5i-side-effects",
        settlement_amount="500.00",
        idempotency_key="settle-5i-side-effects",
    )

    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == before_allocations
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": invoice.invoice_id}) == before_invoice_status
    assert before_invoice_status == "partially_paid"
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase5i_has_no_live_provider_frontend_or_production_enablement():
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
