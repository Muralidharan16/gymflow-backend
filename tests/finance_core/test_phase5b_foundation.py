from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import AsyncSessionLocal
from app.finance_core.models import (
    FinanceAuditEvent,
    FinanceBankAccount,
    FinanceBillingParty,
    FinanceBrand,
    FinanceBrandRefSeries,
    FinanceCreditNote,
    FinanceCreditNoteLine,
    FinanceDivision,
    FinanceGstRegistration,
    FinanceIdempotencyKey,
    FinanceInvoice,
    FinanceInvoiceLine,
    FinanceInvoiceSeries,
    FinanceLedgerAccount,
    FinanceLedgerEntry,
    FinanceLedgerEntryLine,
    FinanceLegalEntity,
    FinanceOutboxEvent,
    FinancePayment,
    FinancePaymentAllocation,
    FinancePaymentEvent,
    FinanceRefund,
    FinanceTaxCode,
    FinanceTaxRecord,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic" / "versions" / "1a2b3c4d5e7f_finance_core_phase_5b_foundation.py"
ADMIN_DATABASE_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")

FINANCE_TABLES = {
    "legal_entities",
    "gst_registrations",
    "divisions",
    "brands",
    "bank_accounts",
    "tax_codes",
    "ledger_accounts",
    "billing_parties",
    "invoice_series",
    "brand_ref_series",
    "invoices",
    "invoice_lines",
    "tax_records",
    "payments",
    "payment_allocations",
    "payment_events",
    "refunds",
    "credit_notes",
    "credit_note_lines",
    "ledger_entries",
    "ledger_entry_lines",
    "audit_events",
    "idempotency_keys",
    "outbox_events",
}

MODEL_TABLES = {
    model.__table__.name
    for model in {
        FinanceAuditEvent,
        FinanceBankAccount,
        FinanceBillingParty,
        FinanceBrand,
        FinanceBrandRefSeries,
        FinanceCreditNote,
        FinanceCreditNoteLine,
        FinanceDivision,
        FinanceGstRegistration,
        FinanceIdempotencyKey,
        FinanceInvoice,
        FinanceInvoiceLine,
        FinanceInvoiceSeries,
        FinanceLedgerAccount,
        FinanceLedgerEntry,
        FinanceLedgerEntryLine,
        FinanceLegalEntity,
        FinanceOutboxEvent,
        FinancePayment,
        FinancePaymentAllocation,
        FinancePaymentEvent,
        FinanceRefund,
        FinanceTaxCode,
        FinanceTaxRecord,
    }
}


async def fetch_all(sql: str, params: dict[str, object] | None = None):
    if ADMIN_DATABASE_URL:
        engine = create_async_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params or {})
                return result.fetchall()
        finally:
            await engine.dispose()

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.fetchall()


async def fetch_scalar(sql: str, params: dict[str, object] | None = None) -> object:
    if ADMIN_DATABASE_URL:
        engine = create_async_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params or {})
                return result.scalar_one()
        finally:
            await engine.dispose()

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


async def expect_db_error(sql: str, params: dict[str, object] | None = None) -> None:
    if ADMIN_DATABASE_URL:
        engine = create_async_engine(ADMIN_DATABASE_URL, poolclass=NullPool)
        try:
            with pytest.raises(Exception):
                async with engine.begin() as conn:
                    await conn.execute(text(sql), params or {})
        finally:
            await engine.dispose()
        return

    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception):
            await session.execute(text(sql), params or {})
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_finance_schema_and_required_tables_exist():
    schema_exists = await fetch_scalar(
        "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'finance')"
    )
    assert schema_exists is True

    rows = await fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'finance'
        ORDER BY table_name
        """
    )
    assert {row[0] for row in rows} == FINANCE_TABLES
    assert MODEL_TABLES == FINANCE_TABLES


@pytest.mark.asyncio
async def test_master_data_relationship_columns_are_present():
    required = {
        "legal_entities": {"id", "code", "legal_name", "pan", "registered_address", "status"},
        "gst_registrations": {"legal_entity_id", "gstin", "state_code", "registered_address"},
        "divisions": {"legal_entity_id", "code", "name"},
        "brands": {"legal_entity_id", "division_id", "code", "name"},
        "billing_parties": {
            "organization_id",
            "billing_name",
            "gst_treatment",
            "gstin",
            "pan",
            "place_of_supply_state_code",
        },
    }
    for table_name, columns in required.items():
        found = {
            row[0]
            for row in await fetch_all(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'finance'
                  AND table_name = :table_name
                """,
                {"table_name": table_name},
            )
        }
        assert columns <= found


@pytest.mark.asyncio
async def test_finance_constraints_cover_numbering_idempotency_outbox_and_money_rules():
    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE connamespace = 'finance'::regnamespace
            """
        )
    }
    assert {
        "uq_finance_invoice_series_scope",
        "uq_finance_brand_ref_series_scope",
        "chk_finance_invoice_series_last_number",
        "chk_finance_brand_ref_series_last_number",
        "uq_finance_invoices_official_number",
        "uq_finance_invoices_brand_reference",
        "chk_finance_invoices_status",
        "chk_finance_invoices_issued_metadata",
        "chk_finance_billing_parties_b2b_requires_gstin",
        "chk_finance_invoice_lines_amounts_nonnegative",
        "chk_finance_invoice_lines_gst_split",
        "chk_finance_payments_status",
        "chk_finance_payments_amount_nonnegative",
        "chk_finance_refunds_status",
        "chk_finance_credit_notes_status",
        "chk_finance_ledger_entry_lines_one_sided",
        "uq_finance_idempotency_keys_scope_key",
        "chk_finance_idempotency_keys_expires_after_create",
        "uq_finance_outbox_events_idempotency",
        "chk_finance_outbox_events_payload_hash",
    } <= constraints


@pytest.mark.asyncio
async def test_invoice_snapshots_and_gst_split_columns_are_present():
    invoice_columns = {
        row[0]
        for row in await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'finance'
              AND table_name = 'invoices'
            """
        )
    }
    assert {
        "seller_legal_name",
        "seller_gstin",
        "seller_pan",
        "seller_registered_address",
        "seller_state_code",
        "buyer_billing_name",
        "buyer_address",
        "buyer_gstin",
        "buyer_pan",
        "buyer_place_of_supply_state_code",
        "buyer_gst_treatment",
        "gst_supply_type",
    } <= invoice_columns

    line_columns = {
        row[0]
        for row in await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'finance'
              AND table_name = 'invoice_lines'
            """
        )
    }
    assert {
        "hsn_sac",
        "taxable_amount",
        "discount_amount",
        "gst_rate_basis_points",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
        "total_tax_amount",
        "line_total_amount",
        "pricing_mode",
    } <= line_columns


@pytest.mark.asyncio
async def test_finance_tables_do_not_store_provider_secrets_or_raw_tokens():
    rows = await fetch_all(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'finance'
          AND (
              column_name ILIKE '%secret%'
              OR column_name ILIKE '%token%'
              OR column_name IN ('raw_body', 'raw_payload', 'api_key', 'card_number', 'cvv')
          )
        ORDER BY table_name, column_name
        """
    )
    assert rows == []


@pytest.mark.asyncio
async def test_security_is_prepared_without_public_finance_api_surface():
    linked_tables = {
        row[0]
        for row in await fetch_all(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = 'finance'
              AND column_name = 'organization_id'
            ORDER BY table_name
            """
        )
    }
    assert {
        "billing_parties",
        "invoices",
        "payments",
        "refunds",
        "credit_notes",
        "audit_events",
        "idempotency_keys",
        "outbox_events",
    } <= linked_tables

    api_files = list((REPO_ROOT / "app").glob("**/*finance*api*.py"))
    assert api_files == []


def test_phase5b_migration_has_no_real_razorpay_or_activation_behavior():
    source = MIGRATION.read_text(encoding="utf-8").lower()
    assert "razorpay" not in source
    assert "activate_subscription" not in source
    assert "platform_subscriptions" not in source
    assert "do not directly activate subscriptions" in source


@pytest.mark.asyncio
async def test_constraints_reject_invalid_b2b_idempotency_outbox_and_ledger_shapes():
    await expect_db_error(
        """
        INSERT INTO finance.billing_parties (
            billing_name, party_type, gst_treatment, billing_address, place_of_supply_state_code
        )
        VALUES ('Invalid B2B', 'business', 'b2b', 'Chennai', '33')
        """
    )
    await expect_db_error(
        """
        INSERT INTO finance.idempotency_keys (
            scope, idempotency_key, request_hash_sha256, status, expires_at
        )
        VALUES ('invoice.issue', 'key-1', :hash, 'processing', clock_timestamp() - interval '1 minute')
        """,
        {"hash": "a" * 64},
    )
    await expect_db_error(
        """
        INSERT INTO finance.outbox_events (
            aggregate_type, aggregate_id, event_type, idempotency_key,
            payload_json, payload_sha256, status, attempt_count
        )
        VALUES (
            'invoice', gen_random_uuid(), 'finance.invoice_issued', 'event-1',
            '[]'::jsonb, :hash, 'pending', 0
        )
        """,
        {"hash": "b" * 64},
    )
