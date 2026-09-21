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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformDispute(Base):
    __tablename__ = "platform_disputes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_provider_releases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    external_dispute_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    dispute_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'opened'"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    financial_hold_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    provider_decision_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    last_evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    evidence_due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    hold_released_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_disputes_payment_attempt_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_disputes_invoice_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_disputes_id_org"),
        UniqueConstraint(
            "provider_code",
            "environment",
            "external_dispute_ref",
            name="uq_platform_disputes_provider_ref",
        ),
        CheckConstraint(
            "provider_code ~ '^[a-z0-9_]+$'",
            name="chk_platform_disputes_provider_code",
        ),
        CheckConstraint(
            "environment IN ('test','live')",
            name="chk_platform_disputes_environment",
        ),
        CheckConstraint(
            "dispute_type IN ('chargeback','cardholder_dispute','duplicate_charge_allegation','fraud_review')",
            name="chk_platform_disputes_type",
        ),
        CheckConstraint(
            "status IN ('opened','evidence_required','submitted','under_review','won','lost','closed')",
            name="chk_platform_disputes_status",
        ),
        CheckConstraint("amount_minor > 0", name="chk_platform_disputes_amount"),
        CheckConstraint(
            "currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'",
            name="chk_platform_disputes_currency",
        ),
        CheckConstraint(
            "last_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_disputes_evidence_sha",
        ),
        CheckConstraint(
            "btrim(last_evidence_ref) <> ''",
            name="chk_platform_disputes_evidence_ref",
        ),
        CheckConstraint(
            "(financial_hold_active AND hold_released_at IS NULL) "
            "OR (NOT financial_hold_active AND hold_released_at IS NOT NULL)",
            name="chk_platform_disputes_hold_shape",
        ),
        CheckConstraint(
            "status NOT IN ('won','lost') "
            "OR (provider_decision_ref IS NOT NULL AND decided_at IS NOT NULL "
            "AND financial_hold_active IS FALSE)",
            name="chk_platform_disputes_decision_shape",
        ),
        CheckConstraint(
            "status <> 'closed' OR closed_at IS NOT NULL",
            name="chk_platform_disputes_closed_shape",
        ),
        CheckConstraint("version >= 1", name="chk_platform_disputes_version"),
        Index("ix_platform_disputes_org_status", "organization_id", "status"),
        Index("ix_platform_disputes_payment", "organization_id", "payment_attempt_id"),
        Index("ix_platform_disputes_invoice", "organization_id", "invoice_id"),
    )


class PlatformDisputeEvidence(Base):
    __tablename__ = "platform_dispute_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dispute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_ack_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["dispute_id", "organization_id"],
            ["platform_disputes.id", "platform_disputes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dispute_evidence_dispute_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_dispute_evidence_id_org"),
        UniqueConstraint(
            "dispute_id",
            "evidence_sha256",
            name="uq_platform_dispute_evidence_sha",
        ),
        CheckConstraint(
            "evidence_kind IN ('provider_notice','customer_statement','invoice','payment_receipt','service_delivery','fraud_signal','provider_decision','chargeback_reversal')",
            name="chk_platform_dispute_evidence_kind",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_dispute_evidence_sha",
        ),
        CheckConstraint(
            "btrim(evidence_ref) <> ''",
            name="chk_platform_dispute_evidence_ref",
        ),
        CheckConstraint(
            "source_type IN ('webhook','reconciliation','manual_review','system')",
            name="chk_platform_dispute_evidence_source",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata_json) = 'object'",
            name="chk_platform_dispute_evidence_metadata",
        ),
        Index("ix_platform_dispute_evidence_dispute", "dispute_id", "created_at"),
    )


class PlatformDisputeEvent(Base):
    __tablename__ = "platform_dispute_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dispute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["dispute_id", "organization_id"],
            ["platform_disputes.id", "platform_disputes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dispute_events_dispute_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_dispute_events_id_org"),
        UniqueConstraint(
            "dispute_id",
            "sequence_number",
            name="uq_platform_dispute_events_sequence",
        ),
        UniqueConstraint(
            "dispute_id",
            "evidence_sha256",
            "event_type",
            name="uq_platform_dispute_events_evidence",
        ),
        CheckConstraint(
            "sequence_number > 0",
            name="chk_platform_dispute_events_sequence",
        ),
        CheckConstraint(
            "source_type IN ('webhook','reconciliation','manual_review','system')",
            name="chk_platform_dispute_events_source",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_dispute_events_evidence_sha",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_dispute_events_payload_sha",
        ),
        CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="chk_platform_dispute_events_payload",
        ),
        Index("ix_platform_dispute_events_dispute", "dispute_id", "sequence_number"),
    )


class PlatformDisputeFinancialEntry(Base):
    __tablename__ = "platform_dispute_financial_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dispute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["dispute_id", "organization_id"],
            ["platform_disputes.id", "platform_disputes.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dispute_financial_entries_dispute_org",
        ),
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_dispute_financial_entries_payment_org",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_platform_dispute_financial_entries_id_org",
        ),
        UniqueConstraint(
            "dispute_id",
            "entry_type",
            name="uq_platform_dispute_financial_entries_type",
        ),
        CheckConstraint(
            "entry_type IN ('liability_recognized','liability_reversed','loss_recognized','loss_reversed')",
            name="chk_platform_dispute_financial_entries_type",
        ),
        CheckConstraint(
            "amount_minor > 0",
            name="chk_platform_dispute_financial_entries_amount",
        ),
        CheckConstraint(
            "currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'",
            name="chk_platform_dispute_financial_entries_currency",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_dispute_financial_entries_evidence_sha",
        ),
        CheckConstraint(
            "btrim(evidence_ref) <> ''",
            name="chk_platform_dispute_financial_entries_evidence_ref",
        ),
        Index(
            "ix_platform_dispute_financial_entries_dispute",
            "dispute_id",
            "effective_at",
        ),
    )


class PlatformFinancialExceptionCase(Base):
    __tablename__ = "platform_financial_exception_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    refund_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    exception_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'detected'"))
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'warning'"))
    external_object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_object_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency_code: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    initial_evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    initial_evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    manual_review_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    automatic_financial_mutation_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    resolution_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolution_detail_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    mapped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_attempt_id", "organization_id"],
            ["platform_payment_attempts.id", "platform_payment_attempts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_financial_exception_payment_org",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "organization_id"],
            ["platform_invoices.id", "platform_invoices.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_financial_exception_invoice_org",
        ),
        ForeignKeyConstraint(
            ["refund_id", "organization_id"],
            ["platform_refunds.id", "platform_refunds.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_financial_exception_refund_org",
        ),
        UniqueConstraint(
            "provider_code",
            "environment",
            "exception_type",
            "external_object_ref",
            "initial_evidence_sha256",
            name="uq_platform_financial_exception_fact",
        ),
        CheckConstraint(
            "provider_code ~ '^[a-z0-9_]+$'",
            name="chk_platform_financial_exception_provider_code",
        ),
        CheckConstraint(
            "environment IN ('test','live')",
            name="chk_platform_financial_exception_environment",
        ),
        CheckConstraint(
            "exception_type IN ('accidental_duplicate_provider_payment','orphan_provider_payment','orphan_settlement','unknown_refund','wrong_customer_mapping','unmapped_dispute')",
            name="chk_platform_financial_exception_type",
        ),
        CheckConstraint(
            "status IN ('detected','quarantined','investigating','mapped','resolved','ignored')",
            name="chk_platform_financial_exception_status",
        ),
        CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="chk_platform_financial_exception_severity",
        ),
        CheckConstraint(
            "amount_minor IS NULL OR amount_minor > 0",
            name="chk_platform_financial_exception_amount",
        ),
        CheckConstraint(
            "currency_code IS NULL OR (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$')",
            name="chk_platform_financial_exception_currency",
        ),
        CheckConstraint(
            "initial_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_financial_exception_evidence_sha",
        ),
        CheckConstraint(
            "btrim(initial_evidence_ref) <> ''",
            name="chk_platform_financial_exception_evidence_ref",
        ),
        CheckConstraint(
            "source_type IN ('webhook','reconciliation','manual_review','system')",
            name="chk_platform_financial_exception_source",
        ),
        CheckConstraint(
            "manual_review_required IS TRUE",
            name="chk_platform_financial_exception_manual_review",
        ),
        CheckConstraint(
            "automatic_financial_mutation_allowed IS FALSE",
            name="chk_platform_financial_exception_no_auto_mutation",
        ),
        CheckConstraint(
            "status <> 'mapped' OR (organization_id IS NOT NULL AND mapped_at IS NOT NULL)",
            name="chk_platform_financial_exception_mapped_shape",
        ),
        CheckConstraint(
            "status NOT IN ('resolved','ignored') "
            "OR (resolution_code IS NOT NULL AND resolved_at IS NOT NULL)",
            name="chk_platform_financial_exception_resolution_shape",
        ),
        CheckConstraint("version >= 1", name="chk_platform_financial_exception_version"),
        Index("ix_platform_financial_exception_status", "status", "severity", "detected_at"),
        Index("ix_platform_financial_exception_org", "organization_id", "status"),
    )


PAY13_MODEL_TABLES = frozenset(
    {
        "platform_disputes",
        "platform_dispute_evidence",
        "platform_dispute_events",
        "platform_dispute_financial_entries",
        "platform_financial_exception_cases",
    }
)
