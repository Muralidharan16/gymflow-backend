from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


SCHEMA = "finance"


class FinanceLegacyRetirementBatch(Base):
    __tablename__ = "legacy_retirement_batches"
    __table_args__ = (
        UniqueConstraint("organization_id", "scope_key", name="uq_pay15_legacy_batch_scope"),
        CheckConstraint(
            "status IN ('inventory','reconciling','ready_for_cutover','cutover','rollback_hold')",
            name="chk_pay15_legacy_batch_status",
        ),
        CheckConstraint(
            "source_invoice_count >= 0 AND source_payment_count >= 0 "
            "AND source_subscription_link_count >= 0",
            name="chk_pay15_legacy_batch_counts",
        ),
        CheckConstraint(
            "source_invoice_total >= 0 AND source_invoice_tax_total >= 0 "
            "AND source_payment_total >= 0",
            name="chk_pay15_legacy_batch_totals",
        ),
        CheckConstraint(
            "source_checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_batch_source_checksum",
        ),
        CheckConstraint(
            "manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_batch_manifest_checksum",
        ),
        CheckConstraint(
            "status NOT IN ('ready_for_cutover','cutover','rollback_hold') "
            "OR manifest_sha256 IS NOT NULL",
            name="chk_pay15_legacy_batch_manifest_required",
        ),
        CheckConstraint(
            "status <> 'cutover' OR (cutover_at IS NOT NULL AND cutover_by IS NOT NULL)",
            name="chk_pay15_legacy_batch_cutover_shape",
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
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'inventory'"))
    source_invoice_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_payment_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_subscription_link_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_invoice_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_invoice_tax_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_payment_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_checksum_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manifest_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    legacy_write_surfaces_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    rollback_strategy: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'finance_pause_legacy_read_only'"),
    )
    cutover_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cutover_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))


class FinanceLegacyInvoiceDisposition(Base):
    __tablename__ = "legacy_invoice_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "legacy_invoice_id", name="uq_pay15_legacy_invoice_source"
        ),
        UniqueConstraint(
            "finance_invoice_id", name="uq_pay15_legacy_invoice_finance_target"
        ),
        CheckConstraint(
            "disposition IN ('historical_read_only','migrated_finance')",
            name="chk_pay15_legacy_invoice_disposition",
        ),
        CheckConstraint(
            "reconciliation_status = 'exact'",
            name="chk_pay15_legacy_invoice_exact",
        ),
        CheckConstraint(
            "disposition <> 'migrated_finance' OR finance_invoice_id IS NOT NULL",
            name="chk_pay15_legacy_invoice_migrated_target",
        ),
        CheckConstraint(
            "disposition <> 'historical_read_only' OR finance_invoice_id IS NULL",
            name="chk_pay15_legacy_invoice_historical_no_target",
        ),
        CheckConstraint(
            "source_subtotal >= 0 AND source_discount_amount >= 0 "
            "AND source_tax_amount >= 0 AND source_total_amount >= 0",
            name="chk_pay15_legacy_invoice_amounts",
        ),
        CheckConstraint(
            "source_currency_code ~ '^[A-Z]{3}$'",
            name="chk_pay15_legacy_invoice_currency",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_invoice_source_sha",
        ),
        CheckConstraint(
            "target_sha256 IS NULL OR target_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_invoice_target_sha",
        ),
        Index("ix_pay15_legacy_invoice_org", "organization_id", "legacy_invoice_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.legacy_retirement_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    legacy_invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    finance_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("finance.invoices.id", ondelete="RESTRICT"), nullable=True
    )
    legacy_invoice_number: Mapped[str] = mapped_column(String(200), nullable=False)
    legacy_payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    legacy_subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


class FinanceLegacyPaymentDisposition(Base):
    __tablename__ = "legacy_payment_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "legacy_payment_id", name="uq_pay15_legacy_payment_source"
        ),
        UniqueConstraint(
            "finance_payment_id", name="uq_pay15_legacy_payment_finance_target"
        ),
        CheckConstraint(
            "disposition IN ('historical_read_only','migrated_finance')",
            name="chk_pay15_legacy_payment_disposition",
        ),
        CheckConstraint(
            "reconciliation_status = 'exact'",
            name="chk_pay15_legacy_payment_exact",
        ),
        CheckConstraint(
            "disposition <> 'migrated_finance' OR finance_payment_id IS NOT NULL",
            name="chk_pay15_legacy_payment_migrated_target",
        ),
        CheckConstraint(
            "disposition <> 'historical_read_only' OR finance_payment_id IS NULL",
            name="chk_pay15_legacy_payment_historical_no_target",
        ),
        CheckConstraint(
            "source_amount >= 0 AND source_discount_amount >= 0",
            name="chk_pay15_legacy_payment_amounts",
        ),
        CheckConstraint(
            "source_currency_code ~ '^[A-Z]{3}$'",
            name="chk_pay15_legacy_payment_currency",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_payment_source_sha",
        ),
        CheckConstraint(
            "target_sha256 IS NULL OR target_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_payment_target_sha",
        ),
        Index("ix_pay15_legacy_payment_org", "organization_id", "legacy_payment_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.legacy_retirement_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    legacy_payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    finance_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("finance.payments.id", ondelete="RESTRICT"), nullable=True
    )
    legacy_subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    source_transaction_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_razorpay_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


class FinanceLegacySubscriptionFinancialLink(Base):
    __tablename__ = "legacy_subscription_financial_links"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "legacy_subscription_id",
            name="uq_pay15_legacy_subscription_link_source",
        ),
        CheckConstraint(
            "disposition IN ('historical_read_only','compatibility_projection','migrated_finance')",
            name="chk_pay15_legacy_subscription_link_disposition",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_subscription_link_sha",
        ),
        Index(
            "ix_pay15_legacy_subscription_link_org",
            "organization_id", "legacy_subscription_id",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.legacy_retirement_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    legacy_subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    modern_subscription_term_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


class FinanceLegacyRetirementAudit(Base):
    __tablename__ = "legacy_retirement_audit"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "sequence_number", name="uq_pay15_legacy_retirement_audit_sequence"
        ),
        CheckConstraint("sequence_number > 0", name="chk_pay15_legacy_retirement_audit_sequence"),
        CheckConstraint(
            "event_type IN ('inventory_captured','invoice_dispositioned','payment_dispositioned',"
            "'subscription_link_preserved','ready_certified','cutover_activated','rollback_hold_activated')",
            name="chk_pay15_legacy_retirement_audit_event",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_pay15_legacy_retirement_audit_sha",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.legacy_retirement_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


PAY15_MODEL_TABLES = frozenset(
    {
        "legacy_retirement_batches",
        "legacy_invoice_dispositions",
        "legacy_payment_dispositions",
        "legacy_subscription_financial_links",
        "legacy_retirement_audit",
    }
)
