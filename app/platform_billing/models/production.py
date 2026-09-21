from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformCatalogRelease(Base):
    __tablename__ = "platform_catalog_releases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    manifest_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        UniqueConstraint("version", name="uq_platform_catalog_releases_version"),
        CheckConstraint("version > 0", name="chk_platform_catalog_releases_version_positive"),
        CheckConstraint("code ~ '^catalog_release_v[1-9][0-9]*$'", name="chk_platform_catalog_releases_code"),
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="chk_platform_catalog_releases_status"),
        CheckConstraint("jsonb_typeof(manifest_json) = 'object'", name="chk_platform_catalog_releases_manifest_object"),
    )


class PlatformCatalogReleaseItem(Base):
    __tablename__ = "platform_catalog_release_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    catalog_release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("platform_catalog_releases.id", ondelete="RESTRICT"), nullable=False
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    price_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True
    )
    item_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint("item_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_catalog_release_items_sha"),
        Index(
            "ux_platform_catalog_release_items_plan_no_price",
            "catalog_release_id",
            "plan_version_id",
            unique=True,
            postgresql_where=text("price_id IS NULL"),
        ),
        Index(
            "ux_platform_catalog_release_items_plan_price",
            "catalog_release_id",
            "plan_version_id",
            "price_id",
            unique=True,
            postgresql_where=text("price_id IS NOT NULL"),
        ),
        Index("ix_platform_catalog_release_items_release", "catalog_release_id"),
    )


class PlatformProviderRelease(Base):
    __tablename__ = "platform_provider_releases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    manifest_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        UniqueConstraint("version", name="uq_platform_provider_releases_version"),
        UniqueConstraint("provider_code", "environment", "adapter_version", name="uq_platform_provider_releases_adapter"),
        CheckConstraint("version > 0", name="chk_platform_provider_releases_version_positive"),
        CheckConstraint("code ~ '^provider_release_v[1-9][0-9]*$'", name="chk_platform_provider_releases_code"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_provider_releases_provider_code"),
        CheckConstraint("environment IN ('test', 'sandbox', 'production')", name="chk_platform_provider_releases_environment"),
        CheckConstraint("contract_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_provider_releases_contract_sha"),
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="chk_platform_provider_releases_status"),
        CheckConstraint("jsonb_typeof(manifest_json) = 'object'", name="chk_platform_provider_releases_manifest_object"),
    )


class PlatformProviderSubscription(Base):
    __tablename__ = "platform_provider_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_subscription_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    canceled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_provider_subscriptions_subscription_org",
        ),
        ForeignKeyConstraint(
            ["provider_customer_id", "organization_id"],
            ["platform_provider_customers.id", "platform_provider_customers.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_provider_subscriptions_customer_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_provider_subscriptions_id_org"),
        UniqueConstraint("provider_code", "external_subscription_ref", name="uq_platform_provider_subscriptions_external"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_provider_subscriptions_provider_code"),
        CheckConstraint("btrim(external_subscription_ref) <> ''", name="chk_platform_provider_subscriptions_external_ref"),
        CheckConstraint(
            "status IN ('pending', 'trialing', 'active', 'past_due', 'paused', 'canceled', 'expired')",
            name="chk_platform_provider_subscriptions_status",
        ),
        CheckConstraint(
            "current_period_end IS NULL OR current_period_start IS NULL OR current_period_end > current_period_start",
            name="chk_platform_provider_subscriptions_period_order",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_provider_subscriptions_evidence_sha",
        ),
        CheckConstraint("version >= 1", name="chk_platform_provider_subscriptions_version"),
        Index("ix_platform_provider_subscriptions_org_subscription", "organization_id", "subscription_id"),
        Index(
            "ux_platform_provider_subscriptions_current",
            "subscription_id",
            "provider_code",
            unique=True,
            postgresql_where=text("status NOT IN ('canceled', 'expired')"),
        ),
    )


class PlatformMandate(Base):
    __tablename__ = "platform_mandates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    provider_customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_mandate_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    mandate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payment_rail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'legacy_provider_recurring'"))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    max_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    replacement_mandate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    replaced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_customer_id", "organization_id"],
            ["platform_provider_customers.id", "platform_provider_customers.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_mandates_customer_org",
        ),
        ForeignKeyConstraint(
            ["provider_subscription_id", "organization_id"],
            ["platform_provider_subscriptions.id", "platform_provider_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_mandates_subscription_org",
        ),
        ForeignKeyConstraint(
            ["payment_method_id", "organization_id"],
            ["platform_payment_methods.id", "platform_payment_methods.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_mandates_payment_method_org",
        ),
        ForeignKeyConstraint(
            ["replacement_mandate_id", "organization_id"],
            ["platform_mandates.id", "platform_mandates.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_mandates_replacement_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_mandates_id_org"),
        UniqueConstraint("provider_code", "external_mandate_ref", name="uq_platform_mandates_external"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_mandates_provider_code"),
        CheckConstraint("btrim(external_mandate_ref) <> ''", name="chk_platform_mandates_external_ref"),
        CheckConstraint("btrim(mandate_type) <> ''", name="chk_platform_mandates_type"),
        CheckConstraint(
            "status IN ('pending', 'authorized', 'active', 'paused', 'revoked', 'expired', 'failed')",
            name="chk_platform_mandates_status",
        ),
        CheckConstraint(
            "payment_rail IN ('upi_autopay', 'e_mandate', 'card_recurring', 'legacy_provider_recurring')",
            name="chk_platform_mandates_payment_rail",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[a-z0-9_]+
        CheckConstraint(
            "currency_code IS NULL OR (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$')",
            name="chk_platform_mandates_currency",
        ),
        CheckConstraint("max_amount_minor IS NULL OR max_amount_minor > 0", name="chk_platform_mandates_max_amount"),
        CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from", name="chk_platform_mandates_valid_window"),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_mandates_evidence_sha",
        ),
        CheckConstraint("version >= 1", name="chk_platform_mandates_version"),
        Index("ix_platform_mandates_org_status", "organization_id", "status"),
    )


class PlatformDocumentSequence(Base):
    __tablename__ = "platform_document_sequences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    legal_entity_code: Mapped[str] = mapped_column(String(60), nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    financial_year: Mapped[str] = mapped_column(String(12), nullable=False)
    series_code: Mapped[str] = mapped_column(String(40), nullable=False)
    prefix: Mapped[str] = mapped_column(String(40), nullable=False)
    padding: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("6"))
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        UniqueConstraint(
            "legal_entity_code", "document_type", "financial_year", "series_code",
            name="uq_platform_document_sequences_identity",
        ),
        CheckConstraint("document_type IN ('invoice', 'credit_note')", name="chk_platform_document_sequences_type"),
        CheckConstraint("btrim(legal_entity_code) <> ''", name="chk_platform_document_sequences_legal_entity"),
        CheckConstraint("btrim(financial_year) <> ''", name="chk_platform_document_sequences_financial_year"),
        CheckConstraint("btrim(series_code) <> ''", name="chk_platform_document_sequences_series"),
        CheckConstraint("btrim(prefix) <> ''", name="chk_platform_document_sequences_prefix"),
        CheckConstraint("padding BETWEEN 1 AND 12", name="chk_platform_document_sequences_padding"),
        CheckConstraint("last_number >= 0", name="chk_platform_document_sequences_last_number"),
        CheckConstraint("status IN ('active', 'closed')", name="chk_platform_document_sequences_status"),
    )


class PlatformInvoice(Base):
    __tablename__ = "platform_invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_document_sequences.id", ondelete="RESTRICT"), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_due_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    service_period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    service_period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    catalog_release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_catalog_releases.id", ondelete="RESTRICT"), nullable=True)
    provider_release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=True)
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=True)
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    commercial_contract_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tax_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    billing_address_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    provider_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_invoice_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["billing_account_id", "organization_id"],
            ["platform_billing_accounts.id", "platform_billing_accounts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoices_billing_account_org",
        ),
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoices_subscription_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_invoices_id_org"),
        UniqueConstraint("document_sequence_id", "invoice_number", name="uq_platform_invoices_sequence_number"),
        UniqueConstraint("provider_code", "provider_invoice_ref", name="uq_platform_invoices_provider_ref"),
        CheckConstraint("status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')", name="chk_platform_invoices_status"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_invoices_currency"),
        CheckConstraint("subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0 AND amount_due_minor >= 0", name="chk_platform_invoices_amounts_nonnegative"),
        CheckConstraint("total_minor = subtotal_minor + tax_minor", name="chk_platform_invoices_total_math"),
        CheckConstraint("amount_due_minor <= total_minor", name="chk_platform_invoices_amount_due"),
        CheckConstraint(
            "(service_period_start IS NULL AND service_period_end IS NULL) OR "
            "(service_period_start IS NOT NULL AND service_period_end IS NOT NULL AND service_period_end > service_period_start)",
            name="chk_platform_invoices_service_period_pair",
        ),
        Index(
            "ux_platform_invoices_subscription_service_period",
            "subscription_id",
            "service_period_start",
            "service_period_end",
            unique=True,
            postgresql_where=text(
                "subscription_id IS NOT NULL AND service_period_start IS NOT NULL "
                "AND service_period_end IS NOT NULL AND status <> 'void'"
            ),
        ),
        CheckConstraint("jsonb_typeof(tax_snapshot_json) = 'object'", name="chk_platform_invoices_tax_snapshot"),
        CheckConstraint("jsonb_typeof(billing_address_snapshot_json) = 'object'", name="chk_platform_invoices_address_snapshot"),
        CheckConstraint("commercial_contract_sha256 IS NULL OR commercial_contract_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_invoices_contract_sha"),
        CheckConstraint("artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_invoices_artifact_sha"),
        CheckConstraint("version >= 1", name="chk_platform_invoices_version"),
        Index("ix_platform_invoices_org_status", "organization_id", "status"),
        Index("ix_platform_invoices_org_issued", "organization_id", text("issued_at DESC")),
    )


class PlatformInvoiceLine(Base):
    __tablename__ = "platform_invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    line_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tax_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=True)
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoice_lines_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_invoice_lines_id_org"),
        UniqueConstraint("invoice_id", "line_number", name="uq_platform_invoice_lines_number"),
        CheckConstraint("line_number > 0", name="chk_platform_invoice_lines_number_positive"),
        CheckConstraint("line_type IN ('subscription', 'addon', 'discount', 'adjustment', 'tax')", name="chk_platform_invoice_lines_type"),
        CheckConstraint("btrim(description) <> ''", name="chk_platform_invoice_lines_description"),
        CheckConstraint("quantity > 0", name="chk_platform_invoice_lines_quantity"),
        CheckConstraint("unit_amount_minor >= 0 AND net_amount_minor >= 0 AND tax_amount_minor >= 0 AND gross_amount_minor >= 0", name="chk_platform_invoice_lines_amounts"),
        CheckConstraint("tax_rate_bps BETWEEN 0 AND 10000", name="chk_platform_invoice_lines_tax_rate"),
        CheckConstraint("gross_amount_minor = net_amount_minor + tax_amount_minor", name="chk_platform_invoice_lines_gross_math"),
        Index("ix_platform_invoice_lines_invoice", "invoice_id", "line_number"),
    )


class PlatformCreditNote(Base):
    __tablename__ = "platform_credit_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_document_sequences.id", ondelete="RESTRICT"), nullable=True)
    credit_note_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_notes_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_credit_notes_id_org"),
        UniqueConstraint("document_sequence_id", "credit_note_number", name="uq_platform_credit_notes_sequence_number"),
        CheckConstraint("status IN ('draft', 'issued', 'applied', 'void')", name="chk_platform_credit_notes_status"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_credit_notes_reason"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_credit_notes_currency"),
        CheckConstraint("subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor > 0", name="chk_platform_credit_notes_amounts"),
        CheckConstraint("total_minor = subtotal_minor + tax_minor", name="chk_platform_credit_notes_total_math"),
        CheckConstraint("artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_credit_notes_artifact_sha"),
        CheckConstraint("version >= 1", name="chk_platform_credit_notes_version"),
        Index("ix_platform_credit_notes_org_invoice", "organization_id", "invoice_id"),
    )


class PlatformCreditNoteLine(Base):
    __tablename__ = "platform_credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    credit_note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    line_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["credit_note_id", "organization_id"],
            ["platform_credit_notes.id", "platform_credit_notes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_note_lines_note_org",
        ),
        ForeignKeyConstraint(
            ["invoice_line_id", "organization_id"],
            ["platform_invoice_lines.id", "platform_invoice_lines.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_note_lines_invoice_line_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_credit_note_lines_id_org"),
        UniqueConstraint("credit_note_id", "line_number", name="uq_platform_credit_note_lines_number"),
        CheckConstraint("line_number > 0", name="chk_platform_credit_note_lines_number_positive"),
        CheckConstraint("line_type IN ('charge_reversal', 'tax_reversal', 'adjustment')", name="chk_platform_credit_note_lines_type"),
        CheckConstraint("btrim(description) <> ''", name="chk_platform_credit_note_lines_description"),
        CheckConstraint("net_amount_minor >= 0 AND tax_amount_minor >= 0 AND gross_amount_minor > 0", name="chk_platform_credit_note_lines_amounts"),
        CheckConstraint("gross_amount_minor = net_amount_minor + tax_amount_minor", name="chk_platform_credit_note_lines_gross_math"),
        Index("ix_platform_credit_note_lines_note", "credit_note_id", "line_number"),
    )


class PlatformPaymentAttempt(Base):
    __tablename__ = "platform_payment_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_payment_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failure_classification: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    provider_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_invoice_org",
        ),
        ForeignKeyConstraint(
            ["provider_operation_id", "organization_id"],
            ["platform_provider_operations.id", "platform_provider_operations.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_operation_org",
        ),
        ForeignKeyConstraint(
            ["mandate_id", "organization_id"],
            ["platform_mandates.id", "platform_mandates.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_mandate_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_payment_attempts_id_org"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_platform_payment_attempts_idempotency"),
        UniqueConstraint("invoice_id", "attempt_number", name="uq_platform_payment_attempts_invoice_attempt"),
        UniqueConstraint("provider_code", "external_payment_ref", name="uq_platform_payment_attempts_provider_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_payment_attempts_provider_code"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_platform_payment_attempts_idempotency"),
        CheckConstraint("attempt_number > 0", name="chk_platform_payment_attempts_attempt_number"),
        CheckConstraint("amount_minor > 0", name="chk_platform_payment_attempts_amount"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_payment_attempts_currency"),
        CheckConstraint(
            "status IN ('requires_action', 'processing', 'succeeded', 'failed', 'unknown', 'canceled')",
            name="chk_platform_payment_attempts_status",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_payment_attempts_evidence_sha",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'canceled') OR completed_at IS NOT NULL",
            name="chk_platform_payment_attempts_terminal_completed",
        ),
        CheckConstraint("version >= 1", name="chk_platform_payment_attempts_version"),
        Index("ix_platform_payment_attempts_org_invoice", "organization_id", "invoice_id"),
        Index("ix_platform_payment_attempts_org_status", "organization_id", "status"),
    )


class PlatformRefund(Base):
    __tablename__ = "platform_refunds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_refund_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    provider_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_payment_attempt_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_invoice_org",
        ),
        ForeignKeyConstraint(
            ["credit_note_id", "organization_id"],
            ["platform_credit_notes.id", "platform_credit_notes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_credit_note_org",
        ),
        ForeignKeyConstraint(
            ["provider_operation_id", "organization_id"],
            ["platform_provider_operations.id", "platform_provider_operations.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_operation_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_refunds_id_org"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_platform_refunds_idempotency"),
        UniqueConstraint("provider_code", "provider_refund_ref", name="uq_platform_refunds_provider_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_refunds_provider_code"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_platform_refunds_idempotency"),
        CheckConstraint("amount_minor > 0", name="chk_platform_refunds_amount"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_refunds_currency"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_refunds_reason"),
        CheckConstraint(
            "status IN ('requested', 'approved', 'provider_pending', 'succeeded', 'failed', 'unknown', 'canceled')",
            name="chk_platform_refunds_status",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_refunds_evidence_sha",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'canceled') OR completed_at IS NOT NULL",
            name="chk_platform_refunds_terminal_completed",
        ),
        CheckConstraint("version >= 1", name="chk_platform_refunds_version"),
        Index("ix_platform_refunds_org_payment", "organization_id", "payment_attempt_id"),
        Index("ix_platform_refunds_org_status", "organization_id", "status"),
    )


PAY11_MODEL_TABLES = frozenset(
    {
        "platform_catalog_releases",
        "platform_catalog_release_items",
        "platform_provider_releases",
        "platform_provider_subscriptions",
        "platform_mandates",
        "platform_document_sequences",
        "platform_invoices",
        "platform_invoice_lines",
        "platform_payment_attempts",
        "platform_refunds",
        "platform_credit_notes",
        "platform_credit_note_lines",
    }
)
",
            name="chk_platform_mandates_failure_code",
        ),
        CheckConstraint(
            "(replacement_mandate_id IS NULL AND replaced_at IS NULL) OR "
            "(replacement_mandate_id IS NOT NULL AND replaced_at IS NOT NULL)",
            name="chk_platform_mandates_replacement_shape",
        ),
        CheckConstraint(
            "replacement_mandate_id IS NULL OR replacement_mandate_id <> id",
            name="chk_platform_mandates_replacement_not_self",
        ),
        CheckConstraint(
            "currency_code IS NULL OR (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$')",
            name="chk_platform_mandates_currency",
        ),
        CheckConstraint("max_amount_minor IS NULL OR max_amount_minor > 0", name="chk_platform_mandates_max_amount"),
        CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from", name="chk_platform_mandates_valid_window"),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_mandates_evidence_sha",
        ),
        CheckConstraint("version >= 1", name="chk_platform_mandates_version"),
        Index("ix_platform_mandates_org_status", "organization_id", "status"),
    )


class PlatformDocumentSequence(Base):
    __tablename__ = "platform_document_sequences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    legal_entity_code: Mapped[str] = mapped_column(String(60), nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    financial_year: Mapped[str] = mapped_column(String(12), nullable=False)
    series_code: Mapped[str] = mapped_column(String(40), nullable=False)
    prefix: Mapped[str] = mapped_column(String(40), nullable=False)
    padding: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("6"))
    last_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        UniqueConstraint(
            "legal_entity_code", "document_type", "financial_year", "series_code",
            name="uq_platform_document_sequences_identity",
        ),
        CheckConstraint("document_type IN ('invoice', 'credit_note')", name="chk_platform_document_sequences_type"),
        CheckConstraint("btrim(legal_entity_code) <> ''", name="chk_platform_document_sequences_legal_entity"),
        CheckConstraint("btrim(financial_year) <> ''", name="chk_platform_document_sequences_financial_year"),
        CheckConstraint("btrim(series_code) <> ''", name="chk_platform_document_sequences_series"),
        CheckConstraint("btrim(prefix) <> ''", name="chk_platform_document_sequences_prefix"),
        CheckConstraint("padding BETWEEN 1 AND 12", name="chk_platform_document_sequences_padding"),
        CheckConstraint("last_number >= 0", name="chk_platform_document_sequences_last_number"),
        CheckConstraint("status IN ('active', 'closed')", name="chk_platform_document_sequences_status"),
    )


class PlatformInvoice(Base):
    __tablename__ = "platform_invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_document_sequences.id", ondelete="RESTRICT"), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_due_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    catalog_release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_catalog_releases.id", ondelete="RESTRICT"), nullable=True)
    provider_release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=True)
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=True)
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    commercial_contract_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tax_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    billing_address_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    provider_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_invoice_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["billing_account_id", "organization_id"],
            ["platform_billing_accounts.id", "platform_billing_accounts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoices_billing_account_org",
        ),
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoices_subscription_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_invoices_id_org"),
        UniqueConstraint("document_sequence_id", "invoice_number", name="uq_platform_invoices_sequence_number"),
        UniqueConstraint("provider_code", "provider_invoice_ref", name="uq_platform_invoices_provider_ref"),
        CheckConstraint("status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')", name="chk_platform_invoices_status"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_invoices_currency"),
        CheckConstraint("subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0 AND amount_due_minor >= 0", name="chk_platform_invoices_amounts_nonnegative"),
        CheckConstraint("total_minor = subtotal_minor + tax_minor", name="chk_platform_invoices_total_math"),
        CheckConstraint("amount_due_minor <= total_minor", name="chk_platform_invoices_amount_due"),
        CheckConstraint("jsonb_typeof(tax_snapshot_json) = 'object'", name="chk_platform_invoices_tax_snapshot"),
        CheckConstraint("jsonb_typeof(billing_address_snapshot_json) = 'object'", name="chk_platform_invoices_address_snapshot"),
        CheckConstraint("commercial_contract_sha256 IS NULL OR commercial_contract_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_invoices_contract_sha"),
        CheckConstraint("artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_invoices_artifact_sha"),
        CheckConstraint("version >= 1", name="chk_platform_invoices_version"),
        Index("ix_platform_invoices_org_status", "organization_id", "status"),
        Index("ix_platform_invoices_org_issued", "organization_id", text("issued_at DESC")),
    )


class PlatformInvoiceLine(Base):
    __tablename__ = "platform_invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    line_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tax_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=True)
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_invoice_lines_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_invoice_lines_id_org"),
        UniqueConstraint("invoice_id", "line_number", name="uq_platform_invoice_lines_number"),
        CheckConstraint("line_number > 0", name="chk_platform_invoice_lines_number_positive"),
        CheckConstraint("line_type IN ('subscription', 'addon', 'discount', 'adjustment', 'tax')", name="chk_platform_invoice_lines_type"),
        CheckConstraint("btrim(description) <> ''", name="chk_platform_invoice_lines_description"),
        CheckConstraint("quantity > 0", name="chk_platform_invoice_lines_quantity"),
        CheckConstraint("unit_amount_minor >= 0 AND net_amount_minor >= 0 AND tax_amount_minor >= 0 AND gross_amount_minor >= 0", name="chk_platform_invoice_lines_amounts"),
        CheckConstraint("tax_rate_bps BETWEEN 0 AND 10000", name="chk_platform_invoice_lines_tax_rate"),
        CheckConstraint("gross_amount_minor = net_amount_minor + tax_amount_minor", name="chk_platform_invoice_lines_gross_math"),
        Index("ix_platform_invoice_lines_invoice", "invoice_id", "line_number"),
    )


class PlatformCreditNote(Base):
    __tablename__ = "platform_credit_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_document_sequences.id", ondelete="RESTRICT"), nullable=True)
    credit_note_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_notes_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_credit_notes_id_org"),
        UniqueConstraint("document_sequence_id", "credit_note_number", name="uq_platform_credit_notes_sequence_number"),
        CheckConstraint("status IN ('draft', 'issued', 'applied', 'void')", name="chk_platform_credit_notes_status"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_credit_notes_reason"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_credit_notes_currency"),
        CheckConstraint("subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor > 0", name="chk_platform_credit_notes_amounts"),
        CheckConstraint("total_minor = subtotal_minor + tax_minor", name="chk_platform_credit_notes_total_math"),
        CheckConstraint("artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_credit_notes_artifact_sha"),
        CheckConstraint("version >= 1", name="chk_platform_credit_notes_version"),
        Index("ix_platform_credit_notes_org_invoice", "organization_id", "invoice_id"),
    )


class PlatformCreditNoteLine(Base):
    __tablename__ = "platform_credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    credit_note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    line_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["credit_note_id", "organization_id"],
            ["platform_credit_notes.id", "platform_credit_notes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_note_lines_note_org",
        ),
        ForeignKeyConstraint(
            ["invoice_line_id", "organization_id"],
            ["platform_invoice_lines.id", "platform_invoice_lines.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_credit_note_lines_invoice_line_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_credit_note_lines_id_org"),
        UniqueConstraint("credit_note_id", "line_number", name="uq_platform_credit_note_lines_number"),
        CheckConstraint("line_number > 0", name="chk_platform_credit_note_lines_number_positive"),
        CheckConstraint("line_type IN ('charge_reversal', 'tax_reversal', 'adjustment')", name="chk_platform_credit_note_lines_type"),
        CheckConstraint("btrim(description) <> ''", name="chk_platform_credit_note_lines_description"),
        CheckConstraint("net_amount_minor >= 0 AND tax_amount_minor >= 0 AND gross_amount_minor > 0", name="chk_platform_credit_note_lines_amounts"),
        CheckConstraint("gross_amount_minor = net_amount_minor + tax_amount_minor", name="chk_platform_credit_note_lines_gross_math"),
        Index("ix_platform_credit_note_lines_note", "credit_note_id", "line_number"),
    )


class PlatformPaymentAttempt(Base):
    __tablename__ = "platform_payment_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_payment_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failure_classification: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    provider_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_invoice_org",
        ),
        ForeignKeyConstraint(
            ["provider_operation_id", "organization_id"],
            ["platform_provider_operations.id", "platform_provider_operations.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_operation_org",
        ),
        ForeignKeyConstraint(
            ["mandate_id", "organization_id"],
            ["platform_mandates.id", "platform_mandates.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_payment_attempts_mandate_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_payment_attempts_id_org"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_platform_payment_attempts_idempotency"),
        UniqueConstraint("invoice_id", "attempt_number", name="uq_platform_payment_attempts_invoice_attempt"),
        UniqueConstraint("provider_code", "external_payment_ref", name="uq_platform_payment_attempts_provider_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_payment_attempts_provider_code"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_platform_payment_attempts_idempotency"),
        CheckConstraint("attempt_number > 0", name="chk_platform_payment_attempts_attempt_number"),
        CheckConstraint("amount_minor > 0", name="chk_platform_payment_attempts_amount"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_payment_attempts_currency"),
        CheckConstraint(
            "status IN ('requires_action', 'processing', 'succeeded', 'failed', 'unknown', 'canceled')",
            name="chk_platform_payment_attempts_status",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_payment_attempts_evidence_sha",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'canceled') OR completed_at IS NOT NULL",
            name="chk_platform_payment_attempts_terminal_completed",
        ),
        CheckConstraint("version >= 1", name="chk_platform_payment_attempts_version"),
        Index("ix_platform_payment_attempts_org_invoice", "organization_id", "invoice_id"),
        Index("ix_platform_payment_attempts_org_status", "organization_id", "status"),
    )


class PlatformRefund(Base):
    __tablename__ = "platform_refunds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_refund_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    provider_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    provider_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_payment_attempt_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_invoice_org",
        ),
        ForeignKeyConstraint(
            ["credit_note_id", "organization_id"],
            ["platform_credit_notes.id", "platform_credit_notes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_credit_note_org",
        ),
        ForeignKeyConstraint(
            ["provider_operation_id", "organization_id"],
            ["platform_provider_operations.id", "platform_provider_operations.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_refunds_operation_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_refunds_id_org"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_platform_refunds_idempotency"),
        UniqueConstraint("provider_code", "provider_refund_ref", name="uq_platform_refunds_provider_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_refunds_provider_code"),
        CheckConstraint("btrim(idempotency_key) <> ''", name="chk_platform_refunds_idempotency"),
        CheckConstraint("amount_minor > 0", name="chk_platform_refunds_amount"),
        CheckConstraint("currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'", name="chk_platform_refunds_currency"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_refunds_reason"),
        CheckConstraint(
            "status IN ('requested', 'approved', 'provider_pending', 'succeeded', 'failed', 'unknown', 'canceled')",
            name="chk_platform_refunds_status",
        ),
        CheckConstraint(
            "provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_refunds_evidence_sha",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'canceled') OR completed_at IS NOT NULL",
            name="chk_platform_refunds_terminal_completed",
        ),
        CheckConstraint("version >= 1", name="chk_platform_refunds_version"),
        Index("ix_platform_refunds_org_payment", "organization_id", "payment_attempt_id"),
        Index("ix_platform_refunds_org_status", "organization_id", "status"),
    )


PAY11_MODEL_TABLES = frozenset(
    {
        "platform_catalog_releases",
        "platform_catalog_release_items",
        "platform_provider_releases",
        "platform_provider_subscriptions",
        "platform_mandates",
        "platform_document_sequences",
        "platform_invoices",
        "platform_invoice_lines",
        "platform_payment_attempts",
        "platform_refunds",
        "platform_credit_notes",
        "platform_credit_note_lines",
    }
)
