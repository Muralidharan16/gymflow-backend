"""finance core phase 5b foundation

Revision ID: 1a2b3c4d5e7f
Revises: 0d4e5f6a7b8c
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "1a2b3c4d5e7f"
down_revision: Union[str, Sequence[str], None] = "0d4e5f6a7b8c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    op.execute("CREATE SCHEMA IF NOT EXISTS finance;")
    op.execute(
        """
        COMMENT ON SCHEMA finance IS
        'Vitara Finance Core owned schema. Phase 5B foundation only; no real provider integration.';
        """
    )

    op.execute(
        """
        CREATE TABLE finance.legal_entities (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(40) NOT NULL,
            legal_name VARCHAR(200) NOT NULL,
            pan VARCHAR(10) NULL,
            registered_address TEXT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_legal_entities_code UNIQUE (code),
            CONSTRAINT chk_finance_legal_entities_code CHECK (code ~ '^[A-Z][A-Z0-9_]*$'),
            CONSTRAINT chk_finance_legal_entities_name CHECK (btrim(legal_name) <> ''),
            CONSTRAINT chk_finance_legal_entities_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.gst_registrations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gstin VARCHAR(15) NOT NULL,
            state_code CHAR(2) NOT NULL,
            state_name VARCHAR(80) NOT NULL,
            registered_address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_gst_registrations_entity_gstin UNIQUE (legal_entity_id, gstin),
            CONSTRAINT chk_finance_gst_registrations_gstin CHECK (gstin ~ '^[0-9]{2}[A-Z0-9]{13}$'),
            CONSTRAINT chk_finance_gst_registrations_state CHECK (state_code ~ '^[0-9]{2}$'),
            CONSTRAINT chk_finance_gst_registrations_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.divisions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            code VARCHAR(10) NOT NULL,
            name VARCHAR(120) NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_divisions_entity_code UNIQUE (legal_entity_id, code),
            CONSTRAINT chk_finance_divisions_code CHECK (code IN ('VS', 'VF')),
            CONSTRAINT chk_finance_divisions_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.brands (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            code VARCHAR(10) NOT NULL,
            name VARCHAR(120) NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_brands_division_code UNIQUE (division_id, code),
            CONSTRAINT chk_finance_brands_code CHECK (code IN ('DS', 'TX', 'FB')),
            CONSTRAINT chk_finance_brands_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.bank_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            account_alias VARCHAR(80) NOT NULL,
            bank_name VARCHAR(120) NOT NULL,
            account_last_four CHAR(4) NULL,
            ifsc VARCHAR(20) NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_bank_accounts_entity_alias UNIQUE (legal_entity_id, account_alias),
            CONSTRAINT chk_finance_bank_accounts_alias CHECK (btrim(account_alias) <> ''),
            CONSTRAINT chk_finance_bank_accounts_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.tax_codes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(40) NOT NULL,
            description TEXT NOT NULL,
            hsn_sac VARCHAR(20) NULL,
            tax_type TEXT NOT NULL DEFAULT 'gst',
            gst_rate_basis_points INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_tax_codes_code UNIQUE (code),
            CONSTRAINT chk_finance_tax_codes_code CHECK (code ~ '^[A-Z0-9_]+$'),
            CONSTRAINT chk_finance_tax_codes_rate_nonnegative CHECK (gst_rate_basis_points >= 0),
            CONSTRAINT chk_finance_tax_codes_type CHECK (tax_type IN ('gst', 'exempt', 'non_gst')),
            CONSTRAINT chk_finance_tax_codes_status CHECK (status IN ('draft', 'active', 'retired'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.ledger_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            code VARCHAR(40) NOT NULL,
            name VARCHAR(160) NOT NULL,
            account_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_ledger_accounts_entity_code UNIQUE (legal_entity_id, code),
            CONSTRAINT chk_finance_ledger_accounts_type CHECK (account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')),
            CONSTRAINT chk_finance_ledger_accounts_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.billing_parties (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            billing_name VARCHAR(200) NOT NULL,
            party_type TEXT NOT NULL,
            gst_treatment TEXT NOT NULL DEFAULT 'b2c',
            gstin VARCHAR(15) NULL,
            pan VARCHAR(10) NULL,
            billing_address TEXT NOT NULL,
            place_of_supply_state_code CHAR(2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_billing_parties_organization UNIQUE (organization_id) DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT chk_finance_billing_parties_party_type CHECK (party_type IN ('individual', 'business', 'government')),
            CONSTRAINT chk_finance_billing_parties_gst_treatment CHECK (gst_treatment IN ('b2c', 'b2b')),
            CONSTRAINT chk_finance_billing_parties_b2b_requires_gstin CHECK (gst_treatment <> 'b2b' OR (gstin IS NOT NULL AND gstin ~ '^[0-9]{2}[A-Z0-9]{13}$')),
            CONSTRAINT chk_finance_billing_parties_place_state CHECK (place_of_supply_state_code ~ '^[0-9]{2}$'),
            CONSTRAINT chk_finance_billing_parties_status CHECK (status IN ('draft', 'active', 'inactive'))
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_billing_parties_org
            ON finance.billing_parties (organization_id)
            WHERE organization_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE finance.invoice_series (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NOT NULL REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            financial_year CHAR(4) NOT NULL,
            series_code VARCHAR(10) NOT NULL,
            last_number BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_invoice_series_scope UNIQUE (legal_entity_id, gst_registration_id, division_id, financial_year, series_code),
            CONSTRAINT chk_finance_invoice_series_code CHECK (series_code IN ('VS', 'VF')),
            CONSTRAINT chk_finance_invoice_series_year CHECK (financial_year ~ '^[0-9]{4}$'),
            CONSTRAINT chk_finance_invoice_series_last_number CHECK (last_number >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.brand_ref_series (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NOT NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            financial_year CHAR(4) NOT NULL,
            series_code VARCHAR(10) NOT NULL,
            last_number BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_brand_ref_series_scope UNIQUE (legal_entity_id, division_id, brand_id, financial_year, series_code),
            CONSTRAINT chk_finance_brand_ref_series_code CHECK (series_code IN ('DS', 'TX', 'FB')),
            CONSTRAINT chk_finance_brand_ref_series_year CHECK (financial_year ~ '^[0-9]{4}$'),
            CONSTRAINT chk_finance_brand_ref_series_last_number CHECK (last_number >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.idempotency_keys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            scope VARCHAR(120) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            request_hash_sha256 CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            response_ref TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_finance_idempotency_keys_scope_key UNIQUE (scope, idempotency_key),
            CONSTRAINT chk_finance_idempotency_keys_scope CHECK (btrim(scope) <> ''),
            CONSTRAINT chk_finance_idempotency_keys_key CHECK (btrim(idempotency_key) <> ''),
            CONSTRAINT chk_finance_idempotency_keys_request_hash CHECK (request_hash_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_finance_idempotency_keys_status CHECK (status IN ('processing', 'succeeded', 'failed')),
            CONSTRAINT chk_finance_idempotency_keys_expires_after_create CHECK (expires_at > created_at)
        );
        """
    )
    op.execute(
        """
        COMMENT ON TABLE finance.idempotency_keys IS
        'Direct Finance Core service idempotency keys. expires_at is required for bounded retention.';
        """
    )
    op.execute(
        """
        CREATE TABLE finance.invoices (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            billing_party_id UUID NOT NULL REFERENCES finance.billing_parties(id) ON DELETE RESTRICT,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NOT NULL REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NOT NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            invoice_series_id UUID NULL REFERENCES finance.invoice_series(id) ON DELETE RESTRICT,
            brand_ref_series_id UUID NULL REFERENCES finance.brand_ref_series(id) ON DELETE RESTRICT,
            financial_year CHAR(4) NOT NULL,
            official_invoice_number VARCHAR(40) NULL,
            brand_reference VARCHAR(40) NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            currency_code CHAR(3) NOT NULL DEFAULT 'INR',
            seller_legal_name VARCHAR(200) NOT NULL,
            seller_gstin VARCHAR(15) NOT NULL,
            seller_pan VARCHAR(10) NULL,
            seller_registered_address TEXT NOT NULL,
            seller_state_code CHAR(2) NOT NULL,
            buyer_billing_name VARCHAR(200) NOT NULL,
            buyer_address TEXT NOT NULL,
            buyer_gstin VARCHAR(15) NULL,
            buyer_pan VARCHAR(10) NULL,
            buyer_place_of_supply_state_code CHAR(2) NOT NULL,
            buyer_gst_treatment TEXT NOT NULL DEFAULT 'b2c',
            gst_supply_type TEXT NOT NULL,
            subtotal_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            discount_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            taxable_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            total_tax_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            grand_total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            issued_at TIMESTAMPTZ NULL,
            cancelled_at TIMESTAMPTZ NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_invoices_official_number UNIQUE (legal_entity_id, gst_registration_id, financial_year, official_invoice_number),
            CONSTRAINT uq_finance_invoices_brand_reference UNIQUE (legal_entity_id, division_id, brand_id, financial_year, brand_reference),
            CONSTRAINT chk_finance_invoices_status CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'overdue', 'cancelled', 'voided', 'credited')),
            CONSTRAINT chk_finance_invoices_supply_type CHECK (gst_supply_type IN ('intra_state', 'inter_state')),
            CONSTRAINT chk_finance_invoices_buyer_treatment CHECK (buyer_gst_treatment IN ('b2c', 'b2b')),
            CONSTRAINT chk_finance_invoices_currency CHECK (currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_finance_invoices_amounts_nonnegative CHECK (subtotal_amount >= 0 AND discount_amount >= 0 AND taxable_amount >= 0 AND total_tax_amount >= 0 AND grand_total_amount >= 0),
            CONSTRAINT chk_finance_invoices_issued_metadata CHECK (status = 'draft' OR (issued_at IS NOT NULL AND official_invoice_number IS NOT NULL)),
            CONSTRAINT chk_finance_invoices_b2b_requires_gstin CHECK (buyer_gst_treatment <> 'b2b' OR (buyer_gstin IS NOT NULL AND buyer_gstin ~ '^[0-9]{2}[A-Z0-9]{13}$'))
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_invoices_org_status
            ON finance.invoices (organization_id, status)
            WHERE organization_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE finance.invoice_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_id UUID NOT NULL REFERENCES finance.invoices(id) ON DELETE RESTRICT,
            line_number INTEGER NOT NULL,
            description TEXT NOT NULL,
            hsn_sac VARCHAR(20) NULL,
            quantity NUMERIC(14, 3) NOT NULL DEFAULT 1,
            unit_amount NUMERIC(14, 2) NOT NULL,
            discount_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            taxable_amount NUMERIC(14, 2) NOT NULL,
            gst_rate_basis_points INTEGER NOT NULL DEFAULT 0,
            cgst_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            sgst_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            igst_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            total_tax_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            line_total_amount NUMERIC(14, 2) NOT NULL,
            pricing_mode TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_invoice_lines_quantity CHECK (quantity > 0),
            CONSTRAINT chk_finance_invoice_lines_pricing_mode CHECK (pricing_mode IN ('tax_exclusive', 'tax_inclusive')),
            CONSTRAINT chk_finance_invoice_lines_amounts_nonnegative CHECK (unit_amount >= 0 AND discount_amount >= 0 AND taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0 AND total_tax_amount >= 0 AND line_total_amount >= 0),
            CONSTRAINT chk_finance_invoice_lines_gst_rate CHECK (gst_rate_basis_points >= 0),
            CONSTRAINT chk_finance_invoice_lines_gst_split CHECK ((cgst_amount = 0 AND sgst_amount = 0) OR igst_amount = 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.tax_records (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_id UUID NOT NULL REFERENCES finance.invoices(id) ON DELETE RESTRICT,
            invoice_line_id UUID NULL REFERENCES finance.invoice_lines(id) ON DELETE RESTRICT,
            tax_code_id UUID NULL REFERENCES finance.tax_codes(id) ON DELETE RESTRICT,
            tax_component TEXT NOT NULL,
            taxable_amount NUMERIC(14, 2) NOT NULL,
            tax_rate_basis_points INTEGER NOT NULL,
            tax_amount NUMERIC(14, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_tax_records_component CHECK (tax_component IN ('cgst', 'sgst', 'igst')),
            CONSTRAINT chk_finance_tax_records_amounts_nonnegative CHECK (taxable_amount >= 0 AND tax_amount >= 0),
            CONSTRAINT chk_finance_tax_records_rate CHECK (tax_rate_basis_points >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.payments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NULL REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            idempotency_key_id UUID NULL REFERENCES finance.idempotency_keys(id) ON DELETE RESTRICT,
            provider_code VARCHAR(40) NOT NULL,
            provider_payment_ref VARCHAR(200) NULL,
            provider_order_ref VARCHAR(200) NULL,
            provider_signature_hash CHAR(64) NULL,
            amount NUMERIC(14, 2) NOT NULL,
            currency_code CHAR(3) NOT NULL DEFAULT 'INR',
            status TEXT NOT NULL DEFAULT 'created',
            raw_status VARCHAR(80) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_payments_provider_payment_ref UNIQUE (provider_code, provider_payment_ref),
            CONSTRAINT chk_finance_payments_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_finance_payments_status CHECK (status IN ('created', 'pending', 'authorized', 'captured', 'failed', 'cancelled', 'refunded', 'partially_refunded', 'settled')),
            CONSTRAINT chk_finance_payments_amount_nonnegative CHECK (amount >= 0),
            CONSTRAINT chk_finance_payments_currency CHECK (currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_finance_payments_signature_hash CHECK (provider_signature_hash IS NULL OR provider_signature_hash ~ '^[0-9a-f]{64}$')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_payments_org_status
            ON finance.payments (organization_id, status)
            WHERE organization_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE finance.payment_allocations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            payment_id UUID NOT NULL REFERENCES finance.payments(id) ON DELETE RESTRICT,
            invoice_id UUID NOT NULL REFERENCES finance.invoices(id) ON DELETE RESTRICT,
            allocated_amount NUMERIC(14, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_payment_allocations_payment_invoice UNIQUE (payment_id, invoice_id),
            CONSTRAINT chk_finance_payment_allocations_amount_nonnegative CHECK (allocated_amount >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.payment_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            payment_id UUID NULL REFERENCES finance.payments(id) ON DELETE RESTRICT,
            provider_code VARCHAR(40) NOT NULL,
            provider_event_id VARCHAR(200) NOT NULL,
            event_type VARCHAR(120) NOT NULL,
            event_payload_sha256 CHAR(64) NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_payment_events_provider_event UNIQUE (provider_code, provider_event_id),
            CONSTRAINT chk_finance_payment_events_payload_hash CHECK (event_payload_sha256 ~ '^[0-9a-f]{64}$')
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.refunds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            payment_id UUID NOT NULL REFERENCES finance.payments(id) ON DELETE RESTRICT,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            amount NUMERIC(14, 2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            reason_code VARCHAR(80) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_refunds_status CHECK (status IN ('requested', 'approved', 'rejected', 'processing', 'succeeded', 'failed', 'cancelled')),
            CONSTRAINT chk_finance_refunds_amount_nonnegative CHECK (amount >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.credit_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            invoice_id UUID NOT NULL REFERENCES finance.invoices(id) ON DELETE RESTRICT,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NOT NULL REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NOT NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            financial_year CHAR(4) NOT NULL,
            credit_note_number VARCHAR(40) NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            issued_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_credit_notes_number UNIQUE (legal_entity_id, gst_registration_id, financial_year, credit_note_number),
            CONSTRAINT chk_finance_credit_notes_status CHECK (status IN ('draft', 'issued', 'voided')),
            CONSTRAINT chk_finance_credit_notes_total_nonnegative CHECK (total_amount >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.credit_note_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            credit_note_id UUID NOT NULL REFERENCES finance.credit_notes(id) ON DELETE RESTRICT,
            invoice_line_id UUID NULL REFERENCES finance.invoice_lines(id) ON DELETE RESTRICT,
            description TEXT NOT NULL,
            amount NUMERIC(14, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_credit_note_lines_amount_nonnegative CHECK (amount >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.ledger_entries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            entry_type TEXT NOT NULL,
            source_type VARCHAR(80) NOT NULL,
            source_id UUID NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            posted_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_ledger_entries_status CHECK (status IN ('draft', 'posted', 'reversed')),
            CONSTRAINT chk_finance_ledger_entries_type CHECK (entry_type IN ('invoice', 'payment', 'refund', 'credit_note', 'settlement', 'adjustment'))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.ledger_entry_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ledger_entry_id UUID NOT NULL REFERENCES finance.ledger_entries(id) ON DELETE RESTRICT,
            ledger_account_id UUID NOT NULL REFERENCES finance.ledger_accounts(id) ON DELETE RESTRICT,
            debit_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            credit_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
            memo TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_ledger_entry_lines_nonnegative CHECK (debit_amount >= 0 AND credit_amount >= 0),
            CONSTRAINT chk_finance_ledger_entry_lines_one_sided CHECK ((debit_amount = 0 AND credit_amount > 0) OR (debit_amount > 0 AND credit_amount = 0))
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.audit_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legal_entity_id UUID NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            actor_id UUID NULL,
            event_type VARCHAR(120) NOT NULL,
            target_type VARCHAR(120) NOT NULL,
            target_id UUID NULL,
            event_payload_sha256 CHAR(64) NOT NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_finance_audit_events_payload_hash CHECK (event_payload_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_finance_audit_events_metadata_object CHECK (jsonb_typeof(metadata_json) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE TABLE finance.outbox_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legal_entity_id UUID NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            aggregate_type VARCHAR(120) NOT NULL,
            aggregate_id UUID NOT NULL,
            event_type VARCHAR(160) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            payload_json JSONB NOT NULL,
            payload_sha256 CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMPTZ NULL,
            published_at TIMESTAMPTZ NULL,
            acknowledged_at TIMESTAMPTZ NULL,
            last_error_code VARCHAR(80) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_finance_outbox_events_idempotency UNIQUE (aggregate_type, aggregate_id, event_type, idempotency_key),
            CONSTRAINT chk_finance_outbox_events_status CHECK (status IN ('pending', 'processing', 'published', 'failed', 'discarded')),
            CONSTRAINT chk_finance_outbox_events_payload_object CHECK (jsonb_typeof(payload_json) = 'object'),
            CONSTRAINT chk_finance_outbox_events_payload_hash CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_finance_outbox_events_attempt_count CHECK (attempt_count >= 0)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_outbox_events_claimable
            ON finance.outbox_events (created_at)
            WHERE status = 'pending';
        """
    )
    op.execute(
        """
        COMMENT ON TABLE finance.outbox_events IS
        'Finance-to-product outbox. Product modules consume these events idempotently; webhooks do not directly activate subscriptions.';
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'test_runner') THEN
                GRANT USAGE ON SCHEMA finance TO test_runner;
                GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
                    ON ALL TABLES IN SCHEMA finance TO test_runner;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS finance CASCADE;")
