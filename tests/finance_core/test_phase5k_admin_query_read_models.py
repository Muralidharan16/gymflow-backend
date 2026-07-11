from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.admin_read_models import MAX_QUERY_LIMIT, PageRequest
from app.finance_core.services.admin_queries import FinanceAdminQueryService
from tests.finance_core.test_phase5c_invoice_engine import create_draft, draft_command, fetch_one, fetch_scalar, issue_invoice
from tests.finance_core.test_phase5d_payment_ledger import payment_command, record_payment, seed_finance_foundation
from tests.finance_core.test_phase5h_invoice_settlement_gate import apply_payment
from tests.finance_core.test_phase5i_settlement_reconciliation import reconcile_payment
from tests.finance_core.test_phase5j_refund_credit_note_reversal import create_credit_note, create_refund_intent


async def finance_read_fixture():
    await seed_finance_foundation()
    draft = await create_draft(draft_command(idempotency_key="draft-5k-issued"))
    issued = await issue_invoice(draft.invoice_id, idempotency_key="issue-5k-issued")
    payment = await record_payment(payment_command(provider_payment_ref="pay_5k", idempotency_key="pay-5k"))
    allocation = await apply_payment(payment.payment_id, issued.invoice_id, idempotency_key="apply-5k")
    settlement = await reconcile_payment(payment.payment_id, settlement_ref="settlement-5k", idempotency_key="settle-5k")
    credit_note = await create_credit_note(issued.invoice_id, credit_note_ref="CN-5K", amount="100.00", idempotency_key="credit-5k")
    refund = await create_refund_intent(payment.payment_id, refund_ref="RF-5K", amount="100.00", credit_note_id=credit_note.credit_note_id, idempotency_key="refund-5k")
    return {
        "invoice_id": issued.invoice_id,
        "payment_id": payment.payment_id,
        "allocation_id": allocation.allocation_id,
        "settlement_ledger_id": settlement.ledger_entry_id,
        "credit_note_id": credit_note.credit_note_id,
        "credit_ledger_id": credit_note.ledger_entry_id,
        "refund_id": refund.refund_id,
    }


@pytest.mark.asyncio
async def test_invoice_summary_query_returns_issued_invoice_details():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        summary = await service.invoice_summary(ids["invoice_id"])

    assert summary is not None
    assert summary.invoice_id == ids["invoice_id"]
    assert summary.status == "paid"
    assert summary.official_invoice_number is not None
    assert summary.brand_reference is not None
    assert summary.grand_total_amount == Decimal("1180.00")
    assert summary.allocated_amount == Decimal("1180.00")
    assert summary.credited_amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_invoice_list_filters_by_status_and_payment_status_with_deterministic_pagination():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        invoices = await service.list_invoices(status="paid", payment_status="captured", page=PageRequest(limit=10))
        empty = await service.list_invoices(status="draft", payment_status="captured", page=PageRequest(limit=10))

    assert [invoice.invoice_id for invoice in invoices] == [ids["invoice_id"]]
    assert empty == []


@pytest.mark.asyncio
async def test_payment_summary_and_payment_list_return_provider_neutral_fields():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        summary = await service.payment_summary(ids["payment_id"])
        payments = await service.list_payments(status="captured", provider_code="test_provider", page=PageRequest(limit=10))

    assert summary is not None
    assert summary.payment_id == ids["payment_id"]
    assert summary.provider_code == "test_provider"
    assert summary.provider_payment_ref == "pay_5k"
    assert summary.amount == Decimal("1180.00")
    assert summary.allocated_amount == Decimal("1180.00")
    assert summary.refunded_amount == Decimal("100.00")
    assert [payment.payment_id for payment in payments] == [ids["payment_id"]]


@pytest.mark.asyncio
async def test_settlement_credit_note_and_refund_history_queries():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        settlements = await service.settlement_history_for_payment(ids["payment_id"])
        invoice_corrections = await service.correction_history(invoice_id=ids["invoice_id"])
        payment_corrections = await service.correction_history(payment_id=ids["payment_id"])

    assert len(settlements) == 1
    assert settlements[0].settlement_ref == "settlement-5k"
    assert settlements[0].settlement_amount == Decimal("1180.00")
    assert [item.item_type for item in invoice_corrections] == ["credit_note"]
    assert invoice_corrections[0].ref == "CN-5K"
    assert [item.item_type for item in payment_corrections] == ["refund"]
    assert payment_corrections[0].ref == "RF-5K"


@pytest.mark.asyncio
async def test_ledger_query_and_account_balance_summary_match_ledger_lines():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        entries = await service.ledger_entries_by_source(source_type="credit_note", source_id=ids["credit_note_id"])
        repeated_entries = await service.ledger_entries_by_source(source_type="credit_note", source_id=ids["credit_note_id"])
        balances = await service.account_balances()

    assert entries == repeated_entries
    assert len(entries) == 1
    assert entries[0].debit_total == entries[0].credit_total == Decimal("100.00")
    by_code = {balance.account_code: balance for balance in balances}
    assert by_code["BANK"].debit_total >= Decimal("1180.00")
    assert by_code["PAYMENT_CLEARING"].credit_total >= Decimal("1180.00")


@pytest.mark.asyncio
async def test_outbox_trace_returns_internal_event_history():
    ids = await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        trace = await service.outbox_trace(aggregate_type="refund", aggregate_id=ids["refund_id"])

    assert len(trace) == 1
    assert trace[0].event_type == "finance.refund.intent.created"
    assert trace[0].payload["refund_id"] == str(ids["refund_id"])


@pytest.mark.asyncio
async def test_pagination_limit_is_enforced_and_query_sorting_is_deterministic():
    await finance_read_fixture()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        invoices = await service.list_invoices(page=PageRequest(limit=MAX_QUERY_LIMIT + 500))
        first = await service.list_invoices(page=PageRequest(limit=1, offset=0))
        second = await service.list_invoices(page=PageRequest(limit=1, offset=1))

    assert len(invoices) <= MAX_QUERY_LIMIT
    assert first
    assert [item.invoice_id for item in first] != [item.invoice_id for item in second]


@pytest.mark.asyncio
async def test_query_service_does_not_mutate_finance_counts():
    ids = await finance_read_fixture()
    before = await _finance_counts()
    async with AsyncSessionLocal() as session:
        service = FinanceAdminQueryService(session)
        await service.invoice_summary(ids["invoice_id"])
        await service.payment_summary(ids["payment_id"])
        await service.list_invoices()
        await service.list_payments()
        await service.settlement_history_for_payment(ids["payment_id"])
        await service.correction_history(invoice_id=ids["invoice_id"])
        await service.ledger_entries_by_source(source_type="credit_note", source_id=ids["credit_note_id"])
        await service.account_balances()
        await service.outbox_trace(aggregate_type="credit_note", aggregate_id=ids["credit_note_id"])
    after = await _finance_counts()
    assert after == before


async def _finance_counts():
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.invoices) AS invoices,
            (SELECT count(*) FROM finance.payments) AS payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.credit_notes) AS credit_notes,
            (SELECT count(*) FROM finance.refunds) AS refunds,
            (SELECT count(*) FROM finance.outbox_events) AS outbox_events
        """
    )


def test_phase5k_has_no_live_provider_frontend_or_production_enablement():
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
