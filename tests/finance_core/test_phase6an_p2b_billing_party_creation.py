from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.finance_core.domain.billing_parties import (
    BILLING_PARTY_CREATION_SOURCE,
    BILLING_PARTY_SYNTHETIC_PHASE,
    BILLING_PARTY_SYNTHETIC_PURPOSE,
    BillingPartyCreationCommand,
    FinanceBillingPartyError,
)
from app.finance_core.domain.invoice_engine import (
    CreateDraftInvoiceCommand,
    FinanceInvoiceValidationError,
    InvoiceLineInput,
    IssueInvoiceCommand,
)
from app.finance_core.services.billing_parties import FinanceBillingPartyCreationService
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine
from tests.finance_core.test_phase5c_invoice_engine import (
    BILLING_PARTY_ID,
    BRAND_ID,
    DIVISION_ID,
    GST_REGISTRATION_ID,
    LEGAL_ENTITY_ID,
    ORG_ID,
    fetch_one,
    fetch_scalar,
    seed_master_data,
)

ORG_B_ID = uuid.UUID("92000000-0000-0000-0000-000000000902")
PROD_ORG_ID = uuid.UUID("92000000-0000-0000-0000-000000000903")
SYNTHETIC_BILLING_NAME = "TEST Razorpay Webhook Smoke Buyer"
SYNTHETIC_ADDRESS = "TEST MODE synthetic billing address, Tamil Nadu"


def metadata() -> dict[str, object]:
    return {
        "test_mode": True,
        "purpose": BILLING_PARTY_SYNTHETIC_PURPOSE,
        "phase": BILLING_PARTY_SYNTHETIC_PHASE,
    }


def command(**overrides) -> BillingPartyCreationCommand:
    values = {
        "organization_id": ORG_ID,
        "actor_organization_id": ORG_ID,
        "billing_name": SYNTHETIC_BILLING_NAME,
        "party_type": "individual",
        "gst_treatment": "b2c",
        "billing_address": SYNTHETIC_ADDRESS,
        "place_of_supply_state_code": "33",
        "status": "active",
        "gstin": None,
        "pan": None,
        "metadata": metadata(),
        "idempotency_key": "phase6an-p2b-create",
        "source": BILLING_PARTY_CREATION_SOURCE,
        "synthetic_mode": True,
    }
    values.update(overrides)
    return BillingPartyCreationCommand(**values)


async def reset_finance_and_orgs() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                TRUNCATE TABLE
                    finance.outbox_events,
                    finance.audit_events,
                    finance.ledger_entry_lines,
                    finance.ledger_entries,
                    finance.credit_note_lines,
                    finance.credit_notes,
                    finance.refunds,
                    finance.payment_events,
                    finance.payment_allocations,
                    finance.payments,
                    finance.tax_records,
                    finance.invoice_lines,
                    finance.invoices,
                    finance.idempotency_keys,
                    finance.brand_ref_series,
                    finance.invoice_series,
                    finance.billing_parties,
                    finance.ledger_accounts,
                    finance.tax_codes,
                    finance.bank_accounts,
                    finance.brands,
                    finance.divisions,
                    finance.gst_registrations,
                    finance.legal_entities
                RESTART IDENTITY CASCADE
                """
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id IN (:org_a, :org_b, :prod_org)"),
            {"org_a": ORG_ID, "org_b": ORG_B_ID, "prod_org": PROD_ORG_ID},
        )
        await session.commit()


async def seed_test_organizations(*, inactive: bool = False) -> None:
    await reset_finance_and_orgs()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES
                    (:org_a, 'Vitara Test Finance Org', 'vitara-test-finance-org', 'basic', :active_a, 10, 'INR'),
                    (:org_b, 'Vitara Sandbox Finance Org', 'vitara-sandbox-finance-org', 'basic', true, 10, 'INR'),
                    (:prod_org, 'Vitara Operations', 'vitara-operations', 'basic', true, 10, 'INR');
                """
            ),
            {"org_a": ORG_ID, "org_b": ORG_B_ID, "prod_org": PROD_ORG_ID, "active_a": not inactive},
        )
        await session.commit()


async def ensure_secondary_test_organization() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES (:org_b, 'Vitara Sandbox Finance Org', 'vitara-sandbox-finance-org', 'basic', true, 10, 'INR')
                ON CONFLICT (id) DO NOTHING;
                """
            ),
            {"org_b": ORG_B_ID},
        )
        await session.commit()


async def create_party(command_: BillingPartyCreationCommand):
    async with AsyncSessionLocal() as session:
        service = FinanceBillingPartyCreationService(session)
        result = await service.create_billing_party(command_)
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_valid_synthetic_billing_party_creation_is_organization_bound_and_sanitized():
    await seed_test_organizations()

    result = await create_party(command())

    row = await fetch_one(
        """
        SELECT organization_id, billing_name, party_type, gst_treatment, gstin, pan,
               place_of_supply_state_code, status, metadata_json
        FROM finance.billing_parties
        WHERE id = :id
        """,
        {"id": result.billing_party_id},
    )
    assert row["organization_id"] == ORG_ID
    assert row["billing_name"] == SYNTHETIC_BILLING_NAME
    assert row["party_type"] == "individual"
    assert row["gst_treatment"] == "b2c"
    assert row["gstin"] is None
    assert row["pan"] is None
    assert row["place_of_supply_state_code"] == "33"
    assert row["status"] == "active"
    assert row["metadata_json"] == metadata()
    assert result.replayed is False
    assert result.billing_label.startswith("TEST")
    assert result.metadata_summary == metadata()
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.payments") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"organization_id": None, "actor_organization_id": None}, "BILLING_PARTY_ORGANIZATION_REQUIRED"),
        ({"organization_id": uuid.uuid4(), "actor_organization_id": uuid.uuid4()}, "BILLING_PARTY_CROSS_ORGANIZATION_REJECTED"),
        ({"billing_name": "   "}, "BILLING_PARTY_NAME_INVALID"),
        ({"billing_name": "x" * 201}, "BILLING_PARTY_NAME_INVALID"),
        ({"billing_name": "TEST\x00Buyer"}, "BILLING_PARTY_NAME_INVALID"),
        ({"billing_address": "x" * 501}, "BILLING_PARTY_ADDRESS_INVALID"),
        ({"party_type": "company"}, "BILLING_PARTY_TYPE_INVALID"),
        ({"gst_treatment": "export"}, "BILLING_PARTY_GST_TREATMENT_INVALID"),
        ({"place_of_supply_state_code": "3A"}, "BILLING_PARTY_STATE_CODE_INVALID"),
        ({"party_type": "business", "gst_treatment": "b2b", "gstin": None}, "BILLING_PARTY_B2B_GSTIN_REQUIRED"),
        ({"gstin": "33ABCDE1234F1Z5"}, "BILLING_PARTY_SYNTHETIC_TAX_ID_REJECTED"),
        ({"pan": "ABCDE1234F"}, "BILLING_PARTY_SYNTHETIC_TAX_ID_REJECTED"),
        ({"metadata": {"organization_id": str(ORG_B_ID), **metadata()}}, "BILLING_PARTY_METADATA_INVALID"),
        ({"source": "browser"}, "BILLING_PARTY_SOURCE_REJECTED"),
    ],
)
async def test_billing_party_creation_validation_rejects_unsafe_commands(overrides, code):
    await seed_test_organizations()

    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command(**overrides))

    assert exc.value.code == code
    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.idempotency_keys") == 0


@pytest.mark.asyncio
async def test_unknown_inactive_and_production_organizations_are_rejected():
    await seed_test_organizations(inactive=True)
    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command())
    assert exc.value.code == "BILLING_PARTY_ORGANIZATION_INACTIVE"

    await seed_test_organizations()
    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command(organization_id=uuid.UUID("92000000-0000-0000-0000-000000000999"), actor_organization_id=uuid.UUID("92000000-0000-0000-0000-000000000999")))
    assert exc.value.code == "BILLING_PARTY_ORGANIZATION_NOT_FOUND"

    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command(organization_id=PROD_ORG_ID, actor_organization_id=PROD_ORG_ID))
    assert exc.value.code == "BILLING_PARTY_SYNTHETIC_ORGANIZATION_REJECTED"
    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties") == 0


@pytest.mark.asyncio
async def test_idempotency_and_duplicate_contracts_are_deterministic():
    await seed_test_organizations()

    first = await create_party(command(idempotency_key="same-key"))
    replay = await create_party(command(idempotency_key="same-key"))
    assert replay.replayed is True
    assert replay.billing_party_id == first.billing_party_id

    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command(idempotency_key="same-key", billing_name="TEST Changed Buyer"))
    assert exc.value.code == "BILLING_PARTY_IDEMPOTENCY_CONFLICT"

    other_key_replay = await create_party(command(idempotency_key="other-key"))
    assert other_key_replay.replayed is True
    assert other_key_replay.billing_party_id == first.billing_party_id

    with pytest.raises(FinanceBillingPartyError) as exc:
        await create_party(command(idempotency_key="changed-key", billing_address="TEST changed synthetic address"))
    assert exc.value.code == "BILLING_PARTY_DUPLICATE_CONFLICT"
    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties WHERE organization_id = :org", {"org": ORG_ID}) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_and_conflicting_creation_respect_organization_lock():
    await seed_test_organizations()

    first, second = await asyncio.gather(
        create_party(command(idempotency_key="concurrent-a")),
        create_party(command(idempotency_key="concurrent-b")),
    )
    assert {first.billing_party_id, second.billing_party_id} == {first.billing_party_id}
    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties WHERE organization_id = :org", {"org": ORG_ID}) == 1

    await reset_finance_and_orgs()
    await seed_test_organizations()

    async def attempt(command_):
        try:
            return await create_party(command_)
        except FinanceBillingPartyError as exc:
            return exc

    results = await asyncio.gather(
        attempt(command(idempotency_key="conflict-a")),
        attempt(command(idempotency_key="conflict-b", billing_address="TEST changed synthetic address")),
    )
    assert sum(not isinstance(result, FinanceBillingPartyError) for result in results) == 1
    assert sum(isinstance(result, FinanceBillingPartyError) and result.code == "BILLING_PARTY_DUPLICATE_CONFLICT" for result in results) == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties WHERE organization_id = :org", {"org": ORG_ID}) == 1


@pytest.mark.asyncio
async def test_late_failure_rolls_back_billing_party_and_idempotency():
    await seed_test_organizations()
    async with AsyncSessionLocal() as session:
        service = FinanceBillingPartyCreationService(session)
        original = service._repo.complete_idempotency_key

        async def fail_complete(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("simulated sanitized billing-party failure")

        service._repo.complete_idempotency_key = fail_complete
        with pytest.raises(RuntimeError, match="simulated sanitized"):
            await service.create_billing_party(command(idempotency_key="rollback-key"))
        await session.rollback()

    assert await fetch_scalar("SELECT count(*) FROM finance.billing_parties") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.idempotency_keys WHERE scope = 'finance.billing_party.create'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.outbox_events") == 0


def invoice_command(*, organization_id=ORG_ID, billing_party_id=BILLING_PARTY_ID, idempotency_key="phase6an-p2b-invoice"):
    return CreateDraftInvoiceCommand(
        organization_id=organization_id,
        legal_entity_id=LEGAL_ENTITY_ID,
        gst_registration_id=GST_REGISTRATION_ID,
        division_id=DIVISION_ID,
        brand_id=BRAND_ID,
        billing_party_id=billing_party_id,
        currency_code="INR",
        supply_date=date(2024, 4, 1),
        line_items=(
            InvoiceLineInput(
                description="TEST subscription",
                quantity=Decimal("1"),
                unit_price=Decimal("1000.00"),
                hsn_sac="998313",
                gst_rate_basis_points=1800,
                pricing_mode="tax_exclusive",
            ),
        ),
        idempotency_key=idempotency_key,
    )


async def invoice_rejection_counts() -> dict[str, int]:
    return dict(
        invoices=await fetch_scalar("SELECT count(*) FROM finance.invoices"),
        lines=await fetch_scalar("SELECT count(*) FROM finance.invoice_lines"),
        taxes=await fetch_scalar("SELECT count(*) FROM finance.tax_records"),
        outbox=await fetch_scalar("SELECT count(*) FROM finance.outbox_events"),
        payments=await fetch_scalar("SELECT count(*) FROM finance.payments"),
        allocations=await fetch_scalar("SELECT count(*) FROM finance.payment_allocations"),
        ledger=await fetch_scalar("SELECT count(*) FROM finance.ledger_entries"),
        invoice_series=await fetch_scalar("SELECT COALESCE(max(last_number), 0) FROM finance.invoice_series"),
        brand_series=await fetch_scalar("SELECT COALESCE(max(last_number), 0) FROM finance.brand_ref_series"),
    )


@pytest.mark.asyncio
async def test_invoice_same_organization_billing_party_succeeds():
    await seed_master_data()
    await ensure_secondary_test_organization()
    async with AsyncSessionLocal() as session:
        result = await FinanceInvoiceEngine(session).create_draft_invoice(invoice_command())
        await session.commit()
    row = await fetch_one("SELECT organization_id, billing_party_id, status FROM finance.invoices WHERE id = :id", {"id": result.invoice_id})
    assert row["organization_id"] == ORG_ID
    assert row["billing_party_id"] == BILLING_PARTY_ID
    assert row["status"] == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup_sql", "command_overrides", "message"),
    [
        ("UPDATE finance.billing_parties SET organization_id = :org_b WHERE id = :billing_party_id", {}, "does not belong"),
        ("UPDATE finance.billing_parties SET organization_id = NULL WHERE id = :billing_party_id", {}, "ownership is required"),
        ("UPDATE finance.billing_parties SET status = 'inactive' WHERE id = :billing_party_id", {}, "not active"),
        ("", {"organization_id": None}, "organization is required"),
        ("", {"organization_id": uuid.UUID("92000000-0000-0000-0000-000000000999")}, "organization was not found"),
        ("UPDATE organizations SET is_active = false WHERE id = :org_a", {}, "organization is not active"),
        ("", {"billing_party_id": uuid.UUID("92000000-0000-0000-0000-000000000999")}, "master data is incomplete"),
    ],
)
async def test_invoice_ownership_rejections_are_zero_mutation(setup_sql, command_overrides, message):
    await seed_master_data()
    if setup_sql and ":org_b" in setup_sql:
        await ensure_secondary_test_organization()
    if setup_sql:
        async with AsyncSessionLocal() as session:
            await session.execute(text(setup_sql), {"org_a": ORG_ID, "org_b": ORG_B_ID, "billing_party_id": BILLING_PARTY_ID})
            await session.commit()
    before = await invoice_rejection_counts()

    async with AsyncSessionLocal() as session:
        with pytest.raises(FinanceInvoiceValidationError, match=message):
            await FinanceInvoiceEngine(session).create_draft_invoice(invoice_command(**command_overrides))
        await session.rollback()

    assert await invoice_rejection_counts() == before


@pytest.mark.asyncio
async def test_issue_invoice_revalidates_billing_party_before_number_consumption():
    await seed_master_data()
    async with AsyncSessionLocal() as session:
        draft = await FinanceInvoiceEngine(session).create_draft_invoice(invoice_command(idempotency_key="issue-guard-draft"))
        await session.commit()
    await ensure_secondary_test_organization()
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE finance.billing_parties SET organization_id = :org_b WHERE id = :billing_party_id"), {"org_b": ORG_B_ID, "billing_party_id": BILLING_PARTY_ID})
        await session.commit()
    before = await invoice_rejection_counts()
    before["invoices"] = 1
    before["lines"] = 1

    async with AsyncSessionLocal() as session:
        with pytest.raises(FinanceInvoiceValidationError, match="does not belong"):
            await FinanceInvoiceEngine(session).issue_invoice(IssueInvoiceCommand(invoice_id=draft.invoice_id, idempotency_key="issue-guard"))
        await session.rollback()

    after = await invoice_rejection_counts()
    assert after["invoice_series"] == 0
    assert after["brand_series"] == 0
    assert after["outbox"] == 0
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": draft.invoice_id}) == "draft"
    assert after == before


@pytest.mark.asyncio
async def test_issue_invoice_revalidates_active_organization_before_number_consumption():
    await seed_master_data()
    async with AsyncSessionLocal() as session:
        draft = await FinanceInvoiceEngine(session).create_draft_invoice(invoice_command(idempotency_key="issue-org-guard-draft"))
        await session.commit()
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE organizations SET is_active = false WHERE id = :org_id"), {"org_id": ORG_ID})
        await session.commit()
    before = await invoice_rejection_counts()
    before["invoices"] = 1
    before["lines"] = 1

    async with AsyncSessionLocal() as session:
        with pytest.raises(FinanceInvoiceValidationError, match="organization is not active"):
            await FinanceInvoiceEngine(session).issue_invoice(IssueInvoiceCommand(invoice_id=draft.invoice_id, idempotency_key="issue-org-guard"))
        await session.rollback()

    after = await invoice_rejection_counts()
    assert after["invoice_series"] == 0
    assert after["brand_series"] == 0
    assert after["outbox"] == 0
    assert await fetch_scalar("SELECT status FROM finance.invoices WHERE id = :id", {"id": draft.invoice_id}) == "draft"
    assert after == before


@pytest.mark.asyncio
async def test_billing_party_metadata_cannot_override_invoice_ownership():
    await seed_master_data()
    await ensure_secondary_test_organization()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE finance.billing_parties
                SET organization_id = :org_b,
                    metadata_json = jsonb_build_object('organization_id', CAST(:org_a AS text), 'test_mode', true)
                WHERE id = :billing_party_id
                """
            ),
            {"org_a": str(ORG_ID), "org_b": ORG_B_ID, "billing_party_id": BILLING_PARTY_ID},
        )
        await session.commit()

    with pytest.raises(FinanceInvoiceValidationError, match="does not belong"):
        async with AsyncSessionLocal() as session:
            await FinanceInvoiceEngine(session).create_draft_invoice(invoice_command(idempotency_key="metadata-no-auth"))
            await session.commit()
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices") == 0
