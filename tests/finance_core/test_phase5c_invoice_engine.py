from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.invoice_engine import (
    CreateDraftInvoiceCommand,
    FinanceInvoiceConflictError,
    FinanceInvoiceStateError,
    InvoiceLineInput,
    IssueInvoiceCommand,
)
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine
from tests.finance_core.admin_database import (
    finance_admin_session,
    truncate_finance_test_tables,
)


ORG_ID = uuid.UUID("91000000-0000-0000-0000-000000000901")
LEGAL_ENTITY_ID = uuid.UUID("91000000-0000-0000-0000-000000000001")
GST_REGISTRATION_ID = uuid.UUID("91000000-0000-0000-0000-000000000101")
DIVISION_ID = uuid.UUID("91000000-0000-0000-0000-000000000201")
BRAND_ID = uuid.UUID("91000000-0000-0000-0000-000000000301")
BILLING_PARTY_ID = uuid.UUID("91000000-0000-0000-0000-000000000401")
B2B_BILLING_PARTY_ID = uuid.UUID("91000000-0000-0000-0000-000000000402")
FY = "2425"


async def cleanup_finance_tables() -> None:
    async with finance_admin_session() as session:
        await truncate_finance_test_tables(session)
        await session.execute(
            text("DELETE FROM organizations WHERE id = :organization_id"),
            {"organization_id": ORG_ID},
        )
        await session.commit()


async def seed_master_data(*, buyer_state_code: str = "33", b2b: bool = False) -> uuid.UUID:
    await cleanup_finance_tables()
    async with finance_admin_session() as session:
        params = {
            "legal_entity_id": LEGAL_ENTITY_ID,
            "gst_registration_id": GST_REGISTRATION_ID,
            "division_id": DIVISION_ID,
            "brand_id": BRAND_ID,
            "billing_party_id": B2B_BILLING_PARTY_ID if b2b else BILLING_PARTY_ID,
            "billing_name": "B2B Buyer Pvt Ltd" if b2b else "B2C Buyer",
            "party_type": "business" if b2b else "individual",
            "gst_treatment": "b2b" if b2b else "b2c",
            "gstin": "29ABCDE1234F1Z5" if b2b else None,
            "pan": "ABCDE1234F" if b2b else None,
            "buyer_state_code": buyer_state_code,
            "financial_year": FY,
            "organization_id": ORG_ID,
        }
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES (:organization_id, 'Vitara Test Finance Org', 'vitara-test-finance-org', 'basic', true, 10, 'INR');
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.legal_entities (
                    id, code, legal_name, pan, registered_address, status
                )
                VALUES (
                    :legal_entity_id, 'VITARA_TEST', 'Vitara Private Limited',
                    'ABCDE1234F', 'Chennai Test Address', 'active'
                );
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.gst_registrations (
                    id, legal_entity_id, gstin, state_code, state_name, registered_address, status
                )
                VALUES (
                    :gst_registration_id, :legal_entity_id, '33ABCDE1234F1Z5',
                    '33', 'Tamil Nadu', 'Chennai GST Address', 'active'
                );
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.divisions (id, legal_entity_id, code, name, status)
                VALUES (:division_id, :legal_entity_id, 'VS', 'Vitara Software', 'active');
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.brands (id, legal_entity_id, division_id, code, name, status)
                VALUES (:brand_id, :legal_entity_id, :division_id, 'DS', 'Doers', 'active');
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.billing_parties (
                    id, organization_id, billing_name, party_type, gst_treatment, gstin, pan,
                    billing_address, place_of_supply_state_code, status
                )
                VALUES (
                    :billing_party_id, :organization_id, :billing_name, :party_type, :gst_treatment,
                    :gstin, :pan, 'Buyer Test Address', :buyer_state_code, 'active'
                );
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.invoice_series (
                    legal_entity_id, gst_registration_id, division_id,
                    financial_year, series_code, last_number
                )
                VALUES (:legal_entity_id, :gst_registration_id, :division_id, :financial_year, 'VS', 0);
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO finance.brand_ref_series (
                    legal_entity_id, division_id, brand_id,
                    financial_year, series_code, last_number
                )
                VALUES (:legal_entity_id, :division_id, :brand_id, :financial_year, 'DS', 0);
                """
            ),
            params,
        )
        await session.commit()
    return B2B_BILLING_PARTY_ID if b2b else BILLING_PARTY_ID


def line(
    *,
    unit_price: str = "1000.00",
    quantity: str = "1",
    discount_amount: str = "0.00",
    pricing_mode: str = "tax_exclusive",
    gst_rate_basis_points: int = 1800,
) -> InvoiceLineInput:
    return InvoiceLineInput(
        description="Doers subscription",
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        discount_amount=Decimal(discount_amount),
        hsn_sac="998313",
        gst_rate_basis_points=gst_rate_basis_points,
        pricing_mode=pricing_mode,
    )


def draft_command(
    *,
    billing_party_id: uuid.UUID = BILLING_PARTY_ID,
    idempotency_key: str = "draft-key-1",
    line_items: tuple[InvoiceLineInput, ...] = (line(),),
) -> CreateDraftInvoiceCommand:
    return CreateDraftInvoiceCommand(
        organization_id=ORG_ID,
        legal_entity_id=LEGAL_ENTITY_ID,
        gst_registration_id=GST_REGISTRATION_ID,
        division_id=DIVISION_ID,
        brand_id=BRAND_ID,
        billing_party_id=billing_party_id,
        currency_code="INR",
        supply_date=date(2024, 4, 1),
        line_items=line_items,
        idempotency_key=idempotency_key,
    )


async def fetch_one(sql: str, params: dict[str, object] | None = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.mappings().one()


async def fetch_scalar(sql: str, params: dict[str, object] | None = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


async def create_draft(command: CreateDraftInvoiceCommand | None = None):
    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        result = await engine.create_draft_invoice(command or draft_command())
        await session.commit()
        return result


async def issue_invoice(invoice_id: uuid.UUID, *, idempotency_key: str = "issue-key-1"):
    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        result = await engine.issue_invoice(IssueInvoiceCommand(invoice_id=invoice_id, idempotency_key=idempotency_key))
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_draft_invoice_has_no_legal_or_brand_number_and_has_preview_totals():
    await seed_master_data()
    result = await create_draft()

    row = await fetch_one("SELECT * FROM finance.invoices WHERE id = :id", {"id": result.invoice_id})
    assert row["status"] == "draft"
    assert row["official_invoice_number"] is None
    assert row["brand_reference"] is None
    assert row["financial_year"] == FY
    assert row["taxable_amount"] == Decimal("1000.00")
    assert row["total_tax_amount"] == Decimal("180.00")
    assert row["grand_total_amount"] == Decimal("1180.00")


@pytest.mark.asyncio
async def test_issue_invoice_allocates_official_number_brand_reference_tax_records_and_outbox():
    await seed_master_data()
    draft = await create_draft()
    issued = await issue_invoice(draft.invoice_id)

    assert issued.status == "issued"
    assert issued.official_invoice_number == "VS/2425/00001"
    assert issued.brand_reference == "DS/2425/00001"

    tax_records = await fetch_scalar("SELECT count(*) FROM finance.tax_records WHERE invoice_id = :id", {"id": draft.invoice_id})
    outbox = await fetch_one("SELECT event_type, status FROM finance.outbox_events WHERE aggregate_id = :id", {"id": draft.invoice_id})
    assert tax_records == 2
    assert outbox["event_type"] == "finance.invoice.issued"
    assert outbox["status"] == "pending"


@pytest.mark.asyncio
async def test_invoice_and_brand_sequences_increment_without_reuse():
    await seed_master_data()
    first = await create_draft(draft_command(idempotency_key="draft-seq-1"))
    second = await create_draft(draft_command(idempotency_key="draft-seq-2"))

    first_issued = await issue_invoice(first.invoice_id, idempotency_key="issue-seq-1")
    second_issued = await issue_invoice(second.invoice_id, idempotency_key="issue-seq-2")

    assert first_issued.official_invoice_number == "VS/2425/00001"
    assert second_issued.official_invoice_number == "VS/2425/00002"
    assert first_issued.brand_reference == "DS/2425/00001"
    assert second_issued.brand_reference == "DS/2425/00002"


@pytest.mark.asyncio
async def test_concurrent_issue_serializes_invoice_and_brand_number_allocation():
    await seed_master_data()
    first = await create_draft(draft_command(idempotency_key="draft-concurrent-1"))
    second = await create_draft(draft_command(idempotency_key="draft-concurrent-2"))

    issued = await asyncio.gather(
        issue_invoice(first.invoice_id, idempotency_key="issue-concurrent-1"),
        issue_invoice(second.invoice_id, idempotency_key="issue-concurrent-2"),
    )

    official_numbers = sorted(result.official_invoice_number for result in issued)
    brand_references = sorted(result.brand_reference for result in issued)
    assert official_numbers == ["VS/2425/00001", "VS/2425/00002"]
    assert brand_references == ["DS/2425/00001", "DS/2425/00002"]
    assert await fetch_scalar("SELECT last_number FROM finance.invoice_series") == 2
    assert await fetch_scalar("SELECT last_number FROM finance.brand_ref_series") == 2


@pytest.mark.asyncio
async def test_rollback_before_commit_does_not_leave_committed_invoice_number():
    await seed_master_data()
    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        draft = await engine.create_draft_invoice(draft_command(idempotency_key="draft-rollback"))
        issued = await engine.issue_invoice(IssueInvoiceCommand(invoice_id=draft.invoice_id, idempotency_key="issue-rollback"))
        assert issued.official_invoice_number == "VS/2425/00001"
        await session.rollback()

    durable_draft = await create_draft(draft_command(idempotency_key="draft-after-rollback"))
    durable_issued = await issue_invoice(durable_draft.invoice_id, idempotency_key="issue-after-rollback")
    assert durable_issued.official_invoice_number == "VS/2425/00001"


@pytest.mark.asyncio
async def test_same_state_uses_cgst_and_sgst_and_different_state_uses_igst():
    await seed_master_data(buyer_state_code="33")
    same_state = await create_draft(draft_command(idempotency_key="draft-same-state"))
    same_line = await fetch_one("SELECT cgst_amount, sgst_amount, igst_amount FROM finance.invoice_lines WHERE invoice_id = :id", {"id": same_state.invoice_id})
    assert same_line["cgst_amount"] == Decimal("90.00")
    assert same_line["sgst_amount"] == Decimal("90.00")
    assert same_line["igst_amount"] == Decimal("0.00")

    await seed_master_data(buyer_state_code="29")
    different_state = await create_draft(draft_command(idempotency_key="draft-different-state"))
    different_line = await fetch_one("SELECT cgst_amount, sgst_amount, igst_amount FROM finance.invoice_lines WHERE invoice_id = :id", {"id": different_state.invoice_id})
    assert different_line["cgst_amount"] == Decimal("0.00")
    assert different_line["sgst_amount"] == Decimal("0.00")
    assert different_line["igst_amount"] == Decimal("180.00")


@pytest.mark.asyncio
async def test_tax_inclusive_tax_exclusive_discount_and_b2b_snapshot_behavior():
    billing_party_id = await seed_master_data(buyer_state_code="29", b2b=True)
    result = await create_draft(
        draft_command(
            billing_party_id=billing_party_id,
            idempotency_key="draft-tax-shapes",
            line_items=(
                line(unit_price="1180.00", pricing_mode="tax_inclusive"),
                line(unit_price="1000.00", discount_amount="100.00", pricing_mode="tax_exclusive"),
            ),
        )
    )
    invoice = await fetch_one(
        """
        SELECT buyer_gst_treatment, buyer_gstin, taxable_amount, total_tax_amount, grand_total_amount
        FROM finance.invoices
        WHERE id = :id
        """,
        {"id": result.invoice_id},
    )
    assert invoice["buyer_gst_treatment"] == "b2b"
    assert invoice["buyer_gstin"] == "29ABCDE1234F1Z5"
    assert invoice["taxable_amount"] == Decimal("1900.00")
    assert invoice["total_tax_amount"] == Decimal("342.00")
    assert invoice["grand_total_amount"] == Decimal("2242.00")


@pytest.mark.asyncio
async def test_idempotent_draft_create_and_conflict_for_changed_payload():
    await seed_master_data()
    first = await create_draft(draft_command(idempotency_key="draft-idem"))
    replay = await create_draft(draft_command(idempotency_key="draft-idem"))
    assert replay.invoice_id == first.invoice_id
    assert replay.replayed is True

    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        with pytest.raises(FinanceInvoiceConflictError):
            await engine.create_draft_invoice(
                draft_command(
                    idempotency_key="draft-idem",
                    line_items=(line(unit_price="2000.00"),),
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_idempotent_issue_does_not_allocate_second_number():
    await seed_master_data()
    draft = await create_draft()
    first = await issue_invoice(draft.invoice_id, idempotency_key="issue-idem")
    replay = await issue_invoice(draft.invoice_id, idempotency_key="issue-idem")
    assert replay.invoice_id == first.invoice_id
    assert replay.official_invoice_number == "VS/2425/00001"
    assert replay.replayed is True
    assert await fetch_scalar("SELECT last_number FROM finance.invoice_series") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events WHERE aggregate_id = :id", {"id": draft.invoice_id}) == 1


@pytest.mark.asyncio
async def test_issued_invoice_is_immutable_through_service():
    await seed_master_data()
    draft = await create_draft()
    await issue_invoice(draft.invoice_id)

    async with AsyncSessionLocal() as session:
        engine = FinanceInvoiceEngine(session)
        with pytest.raises(FinanceInvoiceStateError):
            await engine.replace_draft_lines(
                invoice_id=draft.invoice_id,
                line_items=(line(unit_price="2000.00"),),
            )
        await session.rollback()


def test_phase5c_does_not_import_live_provider_or_subscription_behavior():
    finance_root = Path(__file__).resolve().parents[2] / "app" / "finance_core"
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
    assert "activate_subscription" not in combined