from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import CHAR

from app.models.base import Base, new_uuid


SCHEMA = "finance"


class FinanceLegalEntity(Base):
    __tablename__ = "legal_entities"
    __table_args__ = (
        UniqueConstraint("code", name="uq_finance_legal_entities_code"),
        CheckConstraint("code ~ '^[A-Z][A-Z0-9_]*$'", name="chk_finance_legal_entities_code"),
        CheckConstraint("btrim(legal_name) <> ''", name="chk_finance_legal_entities_name"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_legal_entities_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    registered_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceGstRegistration(Base):
    __tablename__ = "gst_registrations"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "gstin", name="uq_finance_gst_registrations_entity_gstin"),
        CheckConstraint("gstin ~ '^[0-9]{2}[A-Z0-9]{13}$'", name="chk_finance_gst_registrations_gstin"),
        CheckConstraint("state_code ~ '^[0-9]{2}$'", name="chk_finance_gst_registrations_state"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_gst_registrations_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    state_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    state_name: Mapped[str] = mapped_column(String(80), nullable=False)
    registered_address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceDivision(Base):
    __tablename__ = "divisions"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "code", name="uq_finance_divisions_entity_code"),
        CheckConstraint("code IN ('VS', 'VF')", name="chk_finance_divisions_code"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_divisions_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceBrand(Base):
    __tablename__ = "brands"
    __table_args__ = (
        UniqueConstraint("division_id", "code", name="uq_finance_brands_division_code"),
        CheckConstraint("code IN ('DS', 'TX', 'FB')", name="chk_finance_brands_code"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_brands_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceBankAccount(Base):
    __tablename__ = "bank_accounts"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "account_alias", name="uq_finance_bank_accounts_entity_alias"),
        CheckConstraint("btrim(account_alias) <> ''", name="chk_finance_bank_accounts_alias"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_bank_accounts_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    account_alias: Mapped[str] = mapped_column(String(80), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_last_four: Mapped[str | None] = mapped_column(CHAR(4), nullable=True)
    ifsc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceTaxCode(Base):
    __tablename__ = "tax_codes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_finance_tax_codes_code"),
        CheckConstraint("code ~ '^[A-Z0-9_]+$'", name="chk_finance_tax_codes_code"),
        CheckConstraint("gst_rate_basis_points >= 0", name="chk_finance_tax_codes_rate_nonnegative"),
        CheckConstraint("tax_type IN ('gst', 'exempt', 'non_gst')", name="chk_finance_tax_codes_type"),
        CheckConstraint("status IN ('draft', 'active', 'retired')", name="chk_finance_tax_codes_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    hsn_sac: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tax_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'gst'"))
    gst_rate_basis_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceLedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "code", name="uq_finance_ledger_accounts_entity_code"),
        CheckConstraint("account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')", name="chk_finance_ledger_accounts_type"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_ledger_accounts_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    account_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceBillingParty(Base):
    __tablename__ = "billing_parties"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_finance_billing_parties_organization", deferrable=True, initially="DEFERRED"),
        CheckConstraint("party_type IN ('individual', 'business', 'government')", name="chk_finance_billing_parties_party_type"),
        CheckConstraint("gst_treatment IN ('b2c', 'b2b')", name="chk_finance_billing_parties_gst_treatment"),
        CheckConstraint(
            "gst_treatment <> 'b2b' OR (gstin IS NOT NULL AND gstin ~ '^[0-9]{2}[A-Z0-9]{13}$')",
            name="chk_finance_billing_parties_b2b_requires_gstin",
        ),
        CheckConstraint("place_of_supply_state_code ~ '^[0-9]{2}$'", name="chk_finance_billing_parties_place_state"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_billing_parties_status"),
        Index("ix_finance_billing_parties_org", "organization_id", postgresql_where=text("organization_id IS NOT NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    billing_name: Mapped[str] = mapped_column(String(200), nullable=False)
    party_type: Mapped[str] = mapped_column(Text, nullable=False)
    gst_treatment: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'b2c'"))
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    billing_address: Mapped[str] = mapped_column(Text, nullable=False)
    place_of_supply_state_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceInvoiceSeries(Base):
    __tablename__ = "invoice_series"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "gst_registration_id", "division_id", "financial_year", "series_code", name="uq_finance_invoice_series_scope"),
        CheckConstraint("series_code IN ('VS', 'VF')", name="chk_finance_invoice_series_code"),
        CheckConstraint("financial_year ~ '^[0-9]{4}$'", name="chk_finance_invoice_series_year"),
        CheckConstraint("last_number >= 0", name="chk_finance_invoice_series_last_number"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gst_registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.gst_registrations.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    financial_year: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    series_code: Mapped[str] = mapped_column(String(10), nullable=False)
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceBrandRefSeries(Base):
    __tablename__ = "brand_ref_series"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "division_id", "brand_id", "financial_year", "series_code", name="uq_finance_brand_ref_series_scope"),
        CheckConstraint("series_code IN ('DS', 'TX', 'FB')", name="chk_finance_brand_ref_series_code"),
        CheckConstraint("financial_year ~ '^[0-9]{4}$'", name="chk_finance_brand_ref_series_year"),
        CheckConstraint("last_number >= 0", name="chk_finance_brand_ref_series_last_number"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=False)
    financial_year: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    series_code: Mapped[str] = mapped_column(String(10), nullable=False)
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceInvoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "gst_registration_id", "financial_year", "official_invoice_number", name="uq_finance_invoices_official_number"),
        UniqueConstraint("legal_entity_id", "division_id", "brand_id", "financial_year", "brand_reference", name="uq_finance_invoices_brand_reference"),
        CheckConstraint("status IN ('draft', 'issued', 'partially_paid', 'paid', 'overdue', 'cancelled', 'voided', 'credited')", name="chk_finance_invoices_status"),
        CheckConstraint("gst_supply_type IN ('intra_state', 'inter_state')", name="chk_finance_invoices_supply_type"),
        CheckConstraint("buyer_gst_treatment IN ('b2c', 'b2b')", name="chk_finance_invoices_buyer_treatment"),
        CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="chk_finance_invoices_currency"),
        CheckConstraint("subtotal_amount >= 0 AND discount_amount >= 0 AND taxable_amount >= 0 AND total_tax_amount >= 0 AND grand_total_amount >= 0", name="chk_finance_invoices_amounts_nonnegative"),
        CheckConstraint("status = 'draft' OR (issued_at IS NOT NULL AND official_invoice_number IS NOT NULL)", name="chk_finance_invoices_issued_metadata"),
        CheckConstraint(
            "buyer_gst_treatment <> 'b2b' OR (buyer_gstin IS NOT NULL AND buyer_gstin ~ '^[0-9]{2}[A-Z0-9]{13}$')",
            name="chk_finance_invoices_b2b_requires_gstin",
        ),
        Index("ix_finance_invoices_org_status", "organization_id", "status", postgresql_where=text("organization_id IS NOT NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    billing_party_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.billing_parties.id", ondelete="RESTRICT"), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gst_registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.gst_registrations.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=False)
    invoice_series_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoice_series.id", ondelete="RESTRICT"), nullable=True)
    brand_ref_series_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brand_ref_series.id", ondelete="RESTRICT"), nullable=True)
    financial_year: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    official_invoice_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    brand_reference: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    seller_legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    seller_gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    seller_pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    seller_registered_address: Mapped[str] = mapped_column(Text, nullable=False)
    seller_state_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    buyer_billing_name: Mapped[str] = mapped_column(String(200), nullable=False)
    buyer_address: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    buyer_pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    buyer_place_of_supply_state_code: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    buyer_gst_treatment: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'b2c'"))
    gst_supply_type: Mapped[str] = mapped_column(Text, nullable=False)
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    total_tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    grand_total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceInvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_finance_invoice_lines_quantity"),
        CheckConstraint("pricing_mode IN ('tax_exclusive', 'tax_inclusive')", name="chk_finance_invoice_lines_pricing_mode"),
        CheckConstraint("unit_amount >= 0 AND discount_amount >= 0 AND taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0 AND total_tax_amount >= 0 AND line_total_amount >= 0", name="chk_finance_invoice_lines_amounts_nonnegative"),
        CheckConstraint("gst_rate_basis_points >= 0", name="chk_finance_invoice_lines_gst_rate"),
        CheckConstraint("(cgst_amount = 0 AND sgst_amount = 0) OR igst_amount = 0", name="chk_finance_invoice_lines_gst_split"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoices.id", ondelete="RESTRICT"), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    hsn_sac: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, server_default=text("1"))
    unit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate_basis_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    total_tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    line_total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    pricing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceTaxRecord(Base):
    __tablename__ = "tax_records"
    __table_args__ = (
        CheckConstraint("tax_component IN ('cgst', 'sgst', 'igst')", name="chk_finance_tax_records_component"),
        CheckConstraint("taxable_amount >= 0 AND tax_amount >= 0", name="chk_finance_tax_records_amounts_nonnegative"),
        CheckConstraint("tax_rate_basis_points >= 0", name="chk_finance_tax_records_rate"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoices.id", ondelete="RESTRICT"), nullable=False)
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoice_lines.id", ondelete="RESTRICT"), nullable=True)
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.tax_codes.id", ondelete="RESTRICT"), nullable=True)
    tax_component: Mapped[str] = mapped_column(Text, nullable=False)
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_rate_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinancePayment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider_code", "provider_payment_ref", name="uq_finance_payments_provider_payment_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_finance_payments_provider_code"),
        CheckConstraint("status IN ('created', 'pending', 'authorized', 'captured', 'failed', 'cancelled', 'refunded', 'partially_refunded', 'settled')", name="chk_finance_payments_status"),
        CheckConstraint("amount >= 0", name="chk_finance_payments_amount_nonnegative"),
        CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="chk_finance_payments_currency"),
        CheckConstraint("provider_signature_hash IS NULL OR provider_signature_hash ~ '^[0-9a-f]{64}$'", name="chk_finance_payments_signature_hash"),
        Index("ix_finance_payments_org_status", "organization_id", "status", postgresql_where=text("organization_id IS NOT NULL")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gst_registration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.gst_registrations.id", ondelete="RESTRICT"), nullable=True)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    idempotency_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.idempotency_keys.id", ondelete="RESTRICT"), nullable=True)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_payment_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_order_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_signature_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'INR'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'created'"))
    raw_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinancePaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="uq_finance_payment_allocations_payment_invoice"),
        CheckConstraint("allocated_amount >= 0", name="chk_finance_payment_allocations_amount_nonnegative"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoices.id", ondelete="RESTRICT"), nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinancePaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider_code", "provider_event_id", name="uq_finance_payment_events_provider_event"),
        CheckConstraint("event_payload_sha256 ~ '^[0-9a-f]{64}$'", name="chk_finance_payment_events_payload_hash"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=True)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceRefund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("status IN ('requested', 'approved', 'rejected', 'processing', 'succeeded', 'failed', 'cancelled')", name="chk_finance_refunds_status"),
        CheckConstraint("amount >= 0", name="chk_finance_refunds_amount_nonnegative"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceCreditNote(Base):
    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint("legal_entity_id", "gst_registration_id", "financial_year", "credit_note_number", name="uq_finance_credit_notes_number"),
        CheckConstraint("status IN ('draft', 'issued', 'voided')", name="chk_finance_credit_notes_status"),
        CheckConstraint("total_amount >= 0", name="chk_finance_credit_notes_total_nonnegative"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoices.id", ondelete="RESTRICT"), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gst_registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.gst_registrations.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=False)
    financial_year: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    credit_note_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceCreditNoteLine(Base):
    __tablename__ = "credit_note_lines"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="chk_finance_credit_note_lines_amount_nonnegative"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    credit_note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.credit_notes.id", ondelete="RESTRICT"), nullable=False)
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.invoice_lines.id", ondelete="RESTRICT"), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceLedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'posted', 'reversed')", name="chk_finance_ledger_entries_status"),
        CheckConstraint("entry_type IN ('invoice', 'payment', 'refund', 'credit_note', 'settlement', 'adjustment')", name="chk_finance_ledger_entries_type"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceLedgerEntryLine(Base):
    __tablename__ = "ledger_entry_lines"
    __table_args__ = (
        CheckConstraint("debit_amount >= 0 AND credit_amount >= 0", name="chk_finance_ledger_entry_lines_nonnegative"),
        CheckConstraint("(debit_amount = 0 AND credit_amount > 0) OR (debit_amount > 0 AND credit_amount = 0)", name="chk_finance_ledger_entry_lines_one_sided"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.ledger_entries.id", ondelete="RESTRICT"), nullable=False)
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.ledger_accounts.id", ondelete="RESTRICT"), nullable=False)
    debit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceAuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("event_payload_sha256 ~ '^[0-9a-f]{64}$'", name="chk_finance_audit_events_payload_hash"),
        CheckConstraint("jsonb_typeof(metadata_json) = 'object'", name="chk_finance_audit_events_metadata_object"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=True)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceIdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_finance_idempotency_keys_scope_key"),
        CheckConstraint("btrim(scope) <> ''", name="chk_finance_idempotency_keys_scope"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_finance_idempotency_keys_key"),
        CheckConstraint("request_hash_sha256 ~ '^[0-9a-f]{64}$'", name="chk_finance_idempotency_keys_request_hash"),
        CheckConstraint("status IN ('processing', 'succeeded', 'failed')", name="chk_finance_idempotency_keys_status"),
        CheckConstraint("expires_at > created_at", name="chk_finance_idempotency_keys_expires_after_create"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'processing'"))
    response_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class FinanceOutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", "event_type", "idempotency_key", name="uq_finance_outbox_events_idempotency"),
        CheckConstraint("status IN ('pending', 'processing', 'published', 'failed', 'discarded')", name="chk_finance_outbox_events_status"),
        CheckConstraint("jsonb_typeof(payload_json) = 'object'", name="chk_finance_outbox_events_payload_object"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="chk_finance_outbox_events_payload_hash"),
        CheckConstraint("attempt_count >= 0", name="chk_finance_outbox_events_attempt_count"),
        Index("ix_finance_outbox_events_claimable", "created_at", postgresql_where=text("status = 'pending'")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=True)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    aggregate_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
