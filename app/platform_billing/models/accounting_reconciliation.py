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


PAY14_OBJECT_TYPES = (
    "captured_payment",
    "settlement",
    "gateway_fee",
    "refund",
    "refund_fee",
    "dispute",
    "chargeback",
    "adjustment",
)

PAY14_MISMATCH_CATEGORIES = (
    "provider_only",
    "local_only",
    "amount_mismatch",
    "currency_mismatch",
    "status_mismatch",
    "settlement_missing",
    "duplicate_provider_object",
    "unknown_provider_object",
    "refund_mismatch",
    "fee_mismatch",
)

PAY14_SAFE_OUTCOMES = (
    "auto_resolved_by_authoritative_evidence",
    "retry_required",
    "manual_review_required",
    "security_incident",
    "accounting_incident",
)


class PlatformAccountingClosureRun(Base):
    __tablename__ = "platform_accounting_closure_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    closure_key: Mapped[str] = mapped_column(String(180), nullable=False)
    period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'collecting'"))
    expected_object_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    observed_object_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    manual_review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    incident_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    resolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    evidence_manifest_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    evidence_manifest_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    ready_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
        UniqueConstraint(
            "provider_code",
            "environment",
            "closure_key",
            name="uq_platform_accounting_closure_runs_key",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_accounting_closure_runs_id_org"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_accounting_closure_provider"),
        CheckConstraint("environment IN ('test','live')", name="chk_platform_accounting_closure_environment"),
        CheckConstraint("btrim(closure_key) <> ''", name="chk_platform_accounting_closure_key"),
        CheckConstraint("period_end > period_start", name="chk_platform_accounting_closure_period"),
        CheckConstraint(
            "status IN ('collecting','reconciling','review_required','ready_to_close','closed','failed')",
            name="chk_platform_accounting_closure_status",
        ),
        CheckConstraint(
            "expected_object_count >= 0 AND observed_object_count >= 0 "
            "AND mismatch_count >= 0 AND retry_count >= 0 "
            "AND manual_review_count >= 0 AND incident_count >= 0 "
            "AND resolved_count >= 0",
            name="chk_platform_accounting_closure_counts",
        ),
        CheckConstraint(
            "evidence_manifest_sha256 IS NULL OR evidence_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_platform_accounting_closure_manifest_sha",
        ),
        CheckConstraint(
            "(evidence_manifest_sha256 IS NULL AND evidence_manifest_ref IS NULL) "
            "OR (evidence_manifest_sha256 IS NOT NULL AND evidence_manifest_ref IS NOT NULL "
            "AND btrim(evidence_manifest_ref) <> '')",
            name="chk_platform_accounting_closure_manifest_pair",
        ),
        CheckConstraint(
            "status <> 'ready_to_close' OR ready_at IS NOT NULL",
            name="chk_platform_accounting_closure_ready_shape",
        ),
        CheckConstraint(
            "status <> 'closed' OR (ready_at IS NOT NULL AND closed_at IS NOT NULL "
            "AND closed_by IS NOT NULL AND evidence_manifest_sha256 IS NOT NULL)",
            name="chk_platform_accounting_closure_closed_shape",
        ),
        CheckConstraint("version >= 1", name="chk_platform_accounting_closure_version"),
        Index("ix_platform_accounting_closure_status", "provider_code", "status", "period_end"),
        Index("ix_platform_accounting_closure_org", "organization_id", "period_end"),
    )


class PlatformAccountingEvidence(Base):
    __tablename__ = "platform_accounting_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    closure_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_accounting_closure_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    local_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    object_ref: Mapped[str] = mapped_column(String(220), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency_code: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    object_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    safe_metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "closure_run_id",
            "side",
            "object_type",
            "object_ref",
            "evidence_sha256",
            name="uq_platform_accounting_evidence_fact",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_accounting_evidence_id_org"),
        CheckConstraint("side IN ('local','provider','settlement')", name="chk_platform_accounting_evidence_side"),
        CheckConstraint(
            "object_type IN ('captured_payment','settlement','gateway_fee','refund','refund_fee','dispute','chargeback','adjustment')",
            name="chk_platform_accounting_evidence_object_type",
        ),
        CheckConstraint("btrim(object_ref) <> ''", name="chk_platform_accounting_evidence_object_ref"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_accounting_evidence_provider"),
        CheckConstraint("environment IN ('test','live')", name="chk_platform_accounting_evidence_environment"),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="chk_platform_accounting_evidence_amount"),
        CheckConstraint("fee_minor IS NULL OR fee_minor >= 0", name="chk_platform_accounting_evidence_fee"),
        CheckConstraint(
            "currency_code IS NULL OR (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$')",
            name="chk_platform_accounting_evidence_currency",
        ),
        CheckConstraint(
            "evidence_kind IN ('local_snapshot','provider_api','provider_statement','provider_settlement','bank_statement','manual_attestation')",
            name="chk_platform_accounting_evidence_kind",
        ),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_accounting_evidence_sha"),
        CheckConstraint("btrim(evidence_ref) <> ''", name="chk_platform_accounting_evidence_ref"),
        CheckConstraint("jsonb_typeof(safe_metadata_json) = 'object'", name="chk_platform_accounting_evidence_metadata"),
        Index("ix_platform_accounting_evidence_run", "closure_run_id", "object_type", "object_ref"),
        Index("ix_platform_accounting_evidence_org", "organization_id", "object_type"),
    )


class PlatformAccountingReconciliationItem(Base):
    __tablename__ = "platform_accounting_reconciliation_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    closure_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_accounting_closure_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reconciliation_key: Mapped[str] = mapped_column(String(320), nullable=False)
    local_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    local_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    settlement_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mismatch_category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    safe_outcome: Mapped[str] = mapped_column(String(80), nullable=False)
    resolution_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    authoritative_evidence_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    automatic_financial_mutation_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    resolution_evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    first_detected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
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
        UniqueConstraint(
            "closure_run_id",
            "reconciliation_key",
            name="uq_platform_accounting_reconciliation_item_key",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_accounting_reconciliation_item_id_org"),
        ForeignKeyConstraint(
            ["local_evidence_id", "organization_id"],
            ["platform_accounting_evidence.id", "platform_accounting_evidence.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_accounting_item_local_evidence_org",
        ),
        ForeignKeyConstraint(
            ["provider_evidence_id", "organization_id"],
            ["platform_accounting_evidence.id", "platform_accounting_evidence.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_accounting_item_provider_evidence_org",
        ),
        ForeignKeyConstraint(
            ["settlement_evidence_id", "organization_id"],
            ["platform_accounting_evidence.id", "platform_accounting_evidence.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_accounting_item_settlement_evidence_org",
        ),
        CheckConstraint(
            "object_type IN ('captured_payment','settlement','gateway_fee','refund','refund_fee','dispute','chargeback','adjustment')",
            name="chk_platform_accounting_item_object_type",
        ),
        CheckConstraint("btrim(reconciliation_key) <> ''", name="chk_platform_accounting_item_key"),
        CheckConstraint(
            "mismatch_category IS NULL OR mismatch_category IN "
            "('provider_only','local_only','amount_mismatch','currency_mismatch','status_mismatch',"
            "'settlement_missing','duplicate_provider_object','unknown_provider_object','refund_mismatch','fee_mismatch')",
            name="chk_platform_accounting_item_mismatch",
        ),
        CheckConstraint(
            "safe_outcome IN ('auto_resolved_by_authoritative_evidence','retry_required','manual_review_required','security_incident','accounting_incident')",
            name="chk_platform_accounting_item_outcome",
        ),
        CheckConstraint(
            "resolution_status IN ('open','retry_pending','under_review','resolved','incident_open')",
            name="chk_platform_accounting_item_resolution_status",
        ),
        CheckConstraint(
            "automatic_financial_mutation_allowed IS FALSE",
            name="chk_platform_accounting_item_no_auto_money_mutation",
        ),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_accounting_item_reason"),
        CheckConstraint(
            "(resolution_evidence_sha256 IS NULL AND resolution_evidence_ref IS NULL) "
            "OR (resolution_evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "AND resolution_evidence_ref IS NOT NULL AND btrim(resolution_evidence_ref) <> '')",
            name="chk_platform_accounting_item_resolution_evidence",
        ),
        CheckConstraint(
            "resolution_status <> 'resolved' OR "
            "(resolved_at IS NOT NULL AND resolution_evidence_sha256 IS NOT NULL)",
            name="chk_platform_accounting_item_resolved_shape",
        ),
        CheckConstraint(
            "safe_outcome <> 'auto_resolved_by_authoritative_evidence' "
            "OR (mismatch_category IS NULL AND authoritative_evidence_complete IS TRUE "
            "AND resolution_status='resolved')",
            name="chk_platform_accounting_item_auto_resolution_shape",
        ),
        CheckConstraint(
            "safe_outcome <> 'retry_required' OR resolution_status IN ('open','retry_pending','resolved')",
            name="chk_platform_accounting_item_retry_shape",
        ),
        CheckConstraint(
            "safe_outcome <> 'manual_review_required' OR resolution_status IN ('open','under_review','resolved')",
            name="chk_platform_accounting_item_manual_shape",
        ),
        CheckConstraint(
            "safe_outcome NOT IN ('security_incident','accounting_incident') "
            "OR resolution_status IN ('incident_open','resolved')",
            name="chk_platform_accounting_item_incident_shape",
        ),
        CheckConstraint("version >= 1", name="chk_platform_accounting_item_version"),
        Index("ix_platform_accounting_item_run_status", "closure_run_id", "resolution_status"),
        Index("ix_platform_accounting_item_org", "organization_id", "resolution_status"),
        Index("ix_platform_accounting_item_mismatch", "mismatch_category", "safe_outcome"),
    )


class PlatformAccountingIncident(Base):
    __tablename__ = "platform_accounting_incidents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    closure_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_accounting_closure_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reconciliation_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    incident_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    incident_code: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["reconciliation_item_id", "organization_id"],
            ["platform_accounting_reconciliation_items.id", "platform_accounting_reconciliation_items.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_accounting_incident_item_org",
        ),
        UniqueConstraint(
            "reconciliation_item_id",
            "incident_type",
            name="uq_platform_accounting_incident_item_type",
        ),
        CheckConstraint(
            "incident_type IN ('security_incident','accounting_incident')",
            name="chk_platform_accounting_incident_type",
        ),
        CheckConstraint("severity IN ('warning','critical')", name="chk_platform_accounting_incident_severity"),
        CheckConstraint("status IN ('open','investigating','resolved')", name="chk_platform_accounting_incident_status"),
        CheckConstraint("btrim(incident_code) <> ''", name="chk_platform_accounting_incident_code"),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_accounting_incident_sha"),
        CheckConstraint("btrim(evidence_ref) <> ''", name="chk_platform_accounting_incident_ref"),
        CheckConstraint(
            "status <> 'resolved' OR (resolved_at IS NOT NULL AND resolution_code IS NOT NULL)",
            name="chk_platform_accounting_incident_resolved_shape",
        ),
        Index("ix_platform_accounting_incident_status", "status", "severity", "opened_at"),
        Index("ix_platform_accounting_incident_org", "organization_id", "status"),
    )


PAY14_MODEL_TABLES = frozenset(
    {
        "platform_accounting_closure_runs",
        "platform_accounting_evidence",
        "platform_accounting_reconciliation_items",
        "platform_accounting_incidents",
    }
)
