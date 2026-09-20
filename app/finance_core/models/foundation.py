from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, text
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
        CheckConstraint("buyer_kind IN ('organization', 'member')", name="chk_finance_billing_parties_buyer_kind"),
        CheckConstraint(
            "(buyer_kind = 'organization' AND member_id IS NULL) OR "
            "(buyer_kind = 'member' AND member_id IS NOT NULL AND organization_id IS NOT NULL AND party_type = 'individual' AND gst_treatment = 'b2c')",
            name="chk_finance_billing_parties_buyer_shape",
        ),
        ForeignKeyConstraint(
            ["member_id", "organization_id"],
            ["members.id", "members.org_id"],
            name="fk_finance_billing_parties_member_org",
            ondelete="RESTRICT",
        ),
        CheckConstraint("party_type IN ('individual', 'business', 'government')", name="chk_finance_billing_parties_party_type"),
        CheckConstraint("gst_treatment IN ('b2c', 'b2b')", name="chk_finance_billing_parties_gst_treatment"),
        CheckConstraint(
            "gst_treatment <> 'b2b' OR (gstin IS NOT NULL AND gstin ~ '^[0-9]{2}[A-Z0-9]{13}$')",
            name="chk_finance_billing_parties_b2b_requires_gstin",
        ),
        CheckConstraint("place_of_supply_state_code ~ '^[0-9]{2}$'", name="chk_finance_billing_parties_place_state"),
        CheckConstraint("status IN ('draft', 'active', 'inactive')", name="chk_finance_billing_parties_status"),
        Index("ix_finance_billing_parties_org", "organization_id", postgresql_where=text("organization_id IS NOT NULL")),
        Index("uq_finance_billing_parties_org_buyer", "organization_id", unique=True, postgresql_where=text("buyer_kind = 'organization'")),
        Index("uq_finance_billing_parties_member_buyer", "organization_id", "member_id", unique=True, postgresql_where=text("buyer_kind = 'member'")),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    buyer_kind: Mapped[str] = mapped_column(Text, nullable=False)
    member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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


class FinanceBranchAccountingProfile(Base):
    __tablename__ = "branch_accounting_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["branch_id", "organization_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_branch_accounting_profiles_branch_org",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="chk_branch_accounting_profiles_status"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="chk_branch_accounting_profiles_window"),
        Index("ix_branch_accounting_profiles_lookup", "organization_id", "branch_id", "status", "effective_from", "effective_until"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    gst_registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.gst_registrations.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    configured_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceMembershipPlanTaxProfile(Base):
    __tablename__ = "membership_plan_tax_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["membership_plan_id", "organization_id"],
            ["membership_plans.id", "membership_plans.org_id"],
            name="fk_membership_plan_tax_profiles_plan_org",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="chk_membership_plan_tax_profiles_status"),
        CheckConstraint("pricing_mode IN ('tax_exclusive', 'tax_inclusive')", name="chk_membership_plan_tax_profiles_pricing_mode"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="chk_membership_plan_tax_profiles_window"),
        Index("ix_membership_plan_tax_profiles_lookup", "organization_id", "membership_plan_id", "status", "effective_from", "effective_until"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    membership_plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tax_code_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.tax_codes.id", ondelete="RESTRICT"), nullable=False)
    pricing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    configured_by: Mapped[str] = mapped_column(Text, nullable=False)
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


class FinanceOfflinePaymentRequest(Base):
    __tablename__ = "offline_payment_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["finance.invoices.id", "finance.invoices.organization_id"],
            name="fk_pay6_offline_invoice_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["payment_id", "organization_id"],
            ["finance.payments.id", "finance.payments.organization_id"],
            name="fk_pay6_offline_payment_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("prepare_command_id", name="uq_pay6_offline_prepare_command"),
        UniqueConstraint("approval_command_id", name="uq_pay6_offline_approval_command"),
        UniqueConstraint("rejection_command_id", name="uq_pay6_offline_rejection_command"),
        UniqueConstraint("payment_id", name="uq_pay6_offline_payment"),
        UniqueConstraint(
            "organization_id",
            "payment_method",
            "reference_code",
            name="uq_pay6_offline_reference",
        ),
        CheckConstraint(
            "payment_method IN ('cash','bank_transfer','cheque')",
            name="chk_pay6_offline_method",
        ),
        CheckConstraint("amount > 0", name="chk_pay6_offline_amount"),
        CheckConstraint(
            "status IN ('prepared','approved','rejected')",
            name="chk_pay6_offline_status",
        ),
        Index(
            "ix_pay6_offline_requests_org_status",
            "organization_id",
            "status",
            "prepared_at",
            "id",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(24), nullable=False)
    reference_code: Mapped[str] = mapped_column(String(120), nullable=False)
    proof_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'prepared'")
    )
    prepared_actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    prepared_actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    prepare_command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.monetary_commands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_actor_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.monetary_commands.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rejected_actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rejected_actor_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rejection_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.monetary_commands.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rejection_reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinanceOfflinePaymentEvent(Base):
    __tablename__ = "offline_payment_events"
    __table_args__ = (
        UniqueConstraint("monetary_command_id", name="uq_pay6_offline_event_command"),
        CheckConstraint(
            "event_type IN ('offline_payment.prepared','offline_payment.approved','offline_payment.rejected')",
            name="chk_pay6_offline_event_type",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    offline_payment_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.offline_payment_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    monetary_command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.monetary_commands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    request_hash_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    proof_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinanceProviderOperation(Base):
    __tablename__ = "provider_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_id", "organization_id"],
            ["finance.payments.id", "finance.payments.organization_id"],
            name="fk_pay8_provider_operation_payment_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id", "provider_code", "environment",
            "operation_type", "idempotency_key",
            name="uq_pay8_provider_operation_key",
        ),
        UniqueConstraint(
            "payment_id", "operation_type",
            name="uq_pay8_provider_operation_payment",
        ),
        UniqueConstraint(
            "provider_code", "environment", "provider_object_id",
            name="uq_pay8_provider_object",
        ),
        CheckConstraint(
            "provider_code ~ '^[a-z0-9_]{1,40}$'",
            name="chk_pay8_provider_code",
        ),
        CheckConstraint(
            "environment IN ('sandbox','test')",
            name="chk_pay8_provider_environment",
        ),
        CheckConstraint(
            "operation_type='create_checkout'",
            name="chk_pay8_provider_operation_type",
        ),
        CheckConstraint(
            "request_hash_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay8_provider_operation_hash",
        ),
        CheckConstraint(
            "status IN ('reserved','in_flight','succeeded',"
            "'failed_retryable','failed_final','unknown')",
            name="chk_pay8_provider_operation_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 20 "
            "AND attempt_count <= max_attempts AND lease_fence >= 0",
            name="chk_pay8_provider_operation_attempts",
        ),
        CheckConstraint(
            "(status='in_flight') = "
            "(lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="chk_pay8_provider_operation_lease",
        ),
        Index(
            "ix_pay8_provider_operations_org_status",
            "organization_id", "status", "updated_at", "id",
        ),
        Index(
            "ix_pay8_provider_operations_recovery",
            "lease_until", "id",
            postgresql_where=text("status='in_flight'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=new_uuid, server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'reserved'")
    )
    provider_object_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )
    last_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinanceProviderWebhookInbox(Base):
    __tablename__ = "provider_webhook_inbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_id", "organization_id"],
            ["finance.payments.id", "finance.payments.organization_id"],
            name="fk_pay8_webhook_payment_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "provider_code", "environment", "provider_event_id",
            name="uq_pay8_webhook_provider_event",
        ),
        CheckConstraint(
            "status IN ('received','processing','processed','retry','dead_letter')",
            name="chk_pay8_webhook_status",
        ),
        CheckConstraint(
            "processing_attempts >= 0 AND max_attempts BETWEEN 1 AND 50 "
            "AND processing_attempts <= max_attempts AND lease_fence >= 0",
            name="chk_pay8_webhook_attempts",
        ),
        CheckConstraint(
            "(status='processing') = "
            "(lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="chk_pay8_webhook_lease",
        ),
        CheckConstraint(
            "(status='processed') = (payment_event_id IS NOT NULL "
            "AND payment_id IS NOT NULL AND organization_id IS NOT NULL "
            "AND processed_at IS NOT NULL)",
            name="chk_pay8_webhook_processed",
        ),
        Index(
            "ix_pay8_webhook_inbox_status",
            "status", "received_at", "id",
        ),
        Index(
            "ix_pay8_webhook_inbox_recovery",
            "lease_until", "id",
            postgresql_where=text("status='processing'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=new_uuid, server_default=text("gen_random_uuid()"),
    )
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    signature_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_order_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_payment_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_amount_subunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    provider_payment_status: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_captured: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_payment_order_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_order_entity_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_order_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_event_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.payment_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'received'")
    )
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("15"))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinancePayment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider_code", "provider_payment_ref", name="uq_finance_payments_provider_payment_ref"),
        UniqueConstraint("id", "currency_code", name="uq_finance_payments_id_currency"),
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



class FinancePaymentContext(Base):
    __tablename__ = "payment_contexts"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_finance_payment_contexts_id_org"),
        UniqueConstraint(
            "organization_id",
            "context_type",
            "business_reference",
            name="uq_finance_payment_contexts_business",
        ),
        CheckConstraint(
            "context_type = 'member_subscription_term'",
            name="chk_finance_payment_contexts_type",
        ),
        CheckConstraint(
            "business_reference LIKE 'subscription_term:%' AND "
            "char_length(business_reference) = 54 AND "
            "pg_catalog.pg_input_is_valid(substring(business_reference from 19), 'uuid')",
            name="chk_finance_payment_contexts_business",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    context_type: Mapped[str] = mapped_column(String(80), nullable=False)
    business_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinanceMemberSubscriptionFinanceBinding(Base):
    __tablename__ = "member_subscription_finance_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_term_id", "organization_id"],
            ["subscription_terms.id", "subscription_terms.org_id"],
            name="fk_pay4_binding_term_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finance_invoice_id", "organization_id"],
            ["finance.invoices.id", "finance.invoices.organization_id"],
            name="fk_pay4_binding_invoice_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finance_payment_context_id", "organization_id"],
            ["finance.payment_contexts.id", "finance.payment_contexts.organization_id"],
            name="fk_pay4_binding_context_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["member_id", "organization_id"],
            ["members.id", "members.org_id"],
            name="fk_pay4_binding_member_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("subscription_term_id", name="uq_pay4_binding_term"),
        UniqueConstraint("finance_invoice_id", name="uq_pay4_binding_invoice"),
        UniqueConstraint("finance_payment_context_id", name="uq_pay4_binding_context"),
        CheckConstraint(
            "jsonb_typeof(plan_snapshot) = 'object'",
            name="chk_pay4_binding_plan_snapshot",
        ),
        CheckConstraint("amount >= 0", name="chk_pay4_binding_amount"),
        CheckConstraint(
            "char_length(currency_code) = 3 AND upper(currency_code) = currency_code "
            "AND currency_code !~ '[^A-Z]'",
            name="chk_pay4_binding_currency",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    finance_invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    finance_payment_context_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class FinanceMemberSubscriptionCheckoutBinding(Base):
    __tablename__ = "member_subscription_checkout_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["member_subscriptions_v2.id", "member_subscriptions_v2.org_id"],
            name="fk_member_subscription_checkout_bindings_subscription_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["finance.invoices.id", "finance.invoices.organization_id"],
            name="fk_member_subscription_checkout_bindings_invoice_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["checkout_intent_id", "organization_id"],
            ["finance.payments.id", "finance.payments.organization_id"],
            name="fk_member_subscription_checkout_bindings_intent_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("subscription_id", name="uq_member_subscription_checkout_bindings_subscription"),
        UniqueConstraint("invoice_id", name="uq_member_subscription_checkout_bindings_invoice"),
        UniqueConstraint("checkout_intent_id", name="uq_member_subscription_checkout_bindings_intent"),
        CheckConstraint("source_table = 'member_subscriptions_v2'", name="chk_member_subscription_checkout_bindings_source_table"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member_subscriptions_v2'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceRefundObligationBinding(Base):
    __tablename__ = "refund_obligation_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["branch_id", "organization_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_refund_obligation_bindings_branch_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["finance.invoices.id", "finance.invoices.organization_id"],
            name="fk_refund_obligation_bindings_invoice_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("invoice_id", name="uq_refund_obligation_bindings_invoice"),
        CheckConstraint("source_table = 'member_subscriptions_v2'", name="chk_refund_obligation_bindings_source_table"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


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


class FinancePaymentApplicationRecord(Base):
    __tablename__ = "payment_application_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_id", "organization_id"],
            ["finance.payments.id", "finance.payments.organization_id"],
            name="fk_pay9_application_payment_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["finance.invoices.id", "finance.invoices.organization_id"],
            name="fk_pay9_application_invoice_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "payment_event_id",
            name="uq_pay9_application_event",
        ),
        CheckConstraint(
            "decision_code IN ("
            "'applied_paid','applied_partial',"
            "'replayed_existing_allocation',"
            "'unapplied_no_checkout_binding',"
            "'unapplied_subscription_binding_missing',"
            "'unapplied_invoice_not_payable',"
            "'unapplied_invoice_already_paid',"
            "'unapplied_currency_mismatch',"
            "'unapplied_relationship_mismatch',"
            "'unapplied_payment_exhausted'"
            ")",
            name="chk_pay9_application_decision",
        ),
        CheckConstraint(
            "allocated_amount >= 0 AND unapplied_amount >= 0 "
            "AND (invoice_outstanding_amount IS NULL "
            "OR invoice_outstanding_amount >= 0)",
            name="chk_pay9_application_amounts",
        ),
        CheckConstraint(
            "(decision_code IN ('applied_paid','applied_partial') "
            "AND invoice_id IS NOT NULL AND allocation_id IS NOT NULL "
            "AND allocated_amount > 0) OR "
            "(decision_code = 'replayed_existing_allocation' "
            "AND invoice_id IS NOT NULL AND allocation_id IS NOT NULL) OR "
            "(decision_code NOT IN ('applied_paid','applied_partial',"
            "'replayed_existing_allocation') "
            "AND allocation_id IS NULL AND allocated_amount = 0)",
            name="chk_pay9_application_shape",
        ),
        Index(
            "ix_pay9_application_payment",
            "organization_id", "payment_id", "created_at", "id",
        ),
        Index(
            "ix_pay9_application_invoice",
            "organization_id", "invoice_id", "created_at", "id",
            postgresql_where=text("invoice_id IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.payment_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    allocation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.payment_allocations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    decision_code: Mapped[str] = mapped_column(String(64), nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    unapplied_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    invoice_outstanding_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    invoice_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


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
        CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="chk_finance_refunds_currency"),
        ForeignKeyConstraint(
            ["payment_id", "currency_code"],
            ["finance.payments.id", "finance.payments.currency_code"],
            name="fk_finance_refunds_payment_currency",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_finance_refunds_payment_reason_not_null",
            "payment_id",
            "reason_code",
            unique=True,
            postgresql_where=text("reason_code IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))


class FinanceRefundExecutionCommand(Base):
    __tablename__ = "refund_execution_commands"
    __table_args__ = (
        UniqueConstraint("refund_id", name="uq_finance_refund_execution_refund"),
        UniqueConstraint("logical_obligation_key", name="uq_finance_refund_execution_logical_key"),
        CheckConstraint("btrim(source_type) <> ''", name="chk_finance_refund_execution_source"),
        CheckConstraint("amount > 0", name="chk_finance_refund_execution_amount"),
        CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="chk_finance_refund_execution_currency"),
        CheckConstraint(
            "status IN ('pending','processing','retry_pending','provider_accepted','reconciliation_pending','succeeded','rejected','dead_lettered','cancelled')",
            name="chk_finance_refund_execution_status",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 20 AND attempt_count BETWEEN 0 AND max_attempts",
            name="chk_finance_refund_execution_attempts",
        ),
        CheckConstraint("lease_fence >= 0", name="chk_finance_refund_execution_lease_fence"),
        CheckConstraint(
            "last_error_code IS NULL OR (last_error_code ~ '^[a-z][a-z0-9_]{0,63}$' AND last_error_code !~ '(bearer|secret|token)')",
            name="chk_finance_refund_execution_error_code",
        ),
        CheckConstraint(
            "(status = 'processing') = (leased_by IS NOT NULL AND leased_until IS NOT NULL)",
            name="chk_finance_refund_execution_lease",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_finance_refund_execution_provider_evidence",
        ),
        Index(
            "ix_finance_refund_execution_claimable",
            "process_after",
            "materialized_at",
            "command_id",
            postgresql_where=text("status IN ('pending','retry_pending')"),
        ),
        Index(
            "ix_finance_refund_execution_processing",
            "leased_until",
            "command_id",
            postgresql_where=text("status = 'processing'"),
        ),
        Index("ix_finance_refund_execution_maintenance", "status", "process_after", "updated_at"),
        {"schema": SCHEMA},
    )

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    refund_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.refunds.id", ondelete="RESTRICT"), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.legal_entities.id", ondelete="RESTRICT"), nullable=False)
    division_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.divisions.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("finance.brands.id", ondelete="RESTRICT"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    logical_obligation_key: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    process_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    leased_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    materialized_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    provider_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_refund_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)


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
        UniqueConstraint("organization_id", "scope", "idempotency_key", name="uq_finance_idempotency_keys_scope_key"),
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


class FinanceMonetaryCommand(Base):
    __tablename__ = "monetary_commands"
    __table_args__ = (
        UniqueConstraint("organization_id", "scope", "idempotency_key", name="uq_finance_monetary_commands_scope_key"),
        CheckConstraint(
            "char_length(scope) BETWEEN 3 AND 120 AND scope ~ '^[a-z][a-z0-9_.]*$'",
            name="chk_finance_monetary_commands_scope",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 200 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'",
            name="chk_finance_monetary_commands_key",
        ),
        CheckConstraint(
            "char_length(request_hash_sha256) = 64 AND request_hash_sha256 ~ '^[0-9a-f]+$'",
            name="chk_finance_monetary_commands_request_hash",
        ),
        CheckConstraint(
            "char_length(business_reference) BETWEEN 1 AND 200 "
            "AND business_reference ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$'",
            name="chk_finance_monetary_commands_business_ref",
        ),
        CheckConstraint(
            "char_length(actor_type) BETWEEN 1 AND 40 AND actor_type ~ '^[a-z][a-z0-9_]*$'",
            name="chk_finance_monetary_commands_actor_type",
        ),
        CheckConstraint(
            "char_length(actor_ref_sha256) = 64 AND actor_ref_sha256 ~ '^[0-9a-f]+$'",
            name="chk_finance_monetary_commands_actor_hash",
        ),
        CheckConstraint(
            "status IN ('processing','unknown','succeeded','failed_deterministic')",
            name="chk_finance_monetary_commands_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR (char_length(error_code) BETWEEN 1 AND 64 "
            "AND error_code ~ '^[a-z][a-z0-9_]*$' "
            "AND error_code !~ '(secret|token|bearer)')",
            name="chk_finance_monetary_commands_error_code",
        ),
        CheckConstraint(
            "response_ref IS NULL OR (char_length(response_ref) BETWEEN 1 AND 200 "
            "AND response_ref ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$')",
            name="chk_finance_monetary_commands_response_ref",
        ),
        CheckConstraint(
            "(status='succeeded') = (response_ref IS NOT NULL)",
            name="chk_finance_monetary_commands_success_response",
        ),
        CheckConstraint(
            "(status='failed_deterministic') = (error_code IS NOT NULL)",
            name="chk_finance_monetary_commands_failure_error",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed_deterministic')) = (completed_at IS NOT NULL)",
            name="chk_finance_monetary_commands_terminal_completed",
        ),
        CheckConstraint(
            "ambiguity_code IS NULL OR (char_length(ambiguity_code) BETWEEN 1 AND 64 "
            "AND ambiguity_code ~ '^[a-z][a-z0-9_]*$' "
            "AND ambiguity_code !~ '(secret|token|bearer)')",
            name="chk_finance_monetary_commands_ambiguity_code",
        ),
        CheckConstraint(
            "(ambiguity_code IS NULL) = (unknown_at IS NULL)",
            name="chk_finance_monetary_commands_ambiguity_pair",
        ),
        CheckConstraint(
            "status <> 'unknown' OR (ambiguity_code IS NOT NULL AND unknown_at IS NOT NULL)",
            name="chk_finance_monetary_commands_unknown_evidence",
        ),
        Index("ix_finance_monetary_commands_status_created", "status", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    business_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_ref_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'processing'"))
    response_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ambiguity_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unknown_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


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
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("15"))
    leased_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
