from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_uuid


class PlatformReconciliationRun(Base):
    __tablename__ = "platform_reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    run_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    claim_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    watermark_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    discrepancy_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    resolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    items: Mapped[list["PlatformReconciliationItem"]] = relationship(back_populates="run")

    __table_args__ = (
        UniqueConstraint("provider_code", "run_identity", name="uq_platform_reconciliation_runs_identity"),
        CheckConstraint("provider_code ~ '^[a-z0-9_]+$'", name="chk_platform_reconciliation_runs_provider_code"),
        CheckConstraint("btrim(run_identity) <> ''", name="chk_platform_reconciliation_runs_identity_nonempty"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed', 'canceled')", name="chk_platform_reconciliation_runs_status"),
        CheckConstraint("claim_state IN ('idle', 'processing')", name="chk_platform_reconciliation_runs_claim_state"),
        CheckConstraint("attempt_count >= 0", name="chk_platform_reconciliation_runs_attempt_count"),
        CheckConstraint(
            "(claimed_at IS NULL AND claim_expires_at IS NULL) OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="chk_platform_reconciliation_runs_claim_timestamps_paired",
        ),
        CheckConstraint(
            "claim_state <> 'processing' OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="chk_platform_reconciliation_runs_processing_claim_metadata",
        ),
        CheckConstraint(
            "claim_state <> 'idle' OR (claimed_at IS NULL AND claim_expires_at IS NULL)",
            name="chk_platform_reconciliation_runs_idle_claim_metadata",
        ),
        CheckConstraint(
            "claim_expires_at IS NULL OR claim_expires_at > claimed_at",
            name="chk_platform_reconciliation_runs_positive_lease",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'canceled') OR claim_state = 'idle'",
            name="chk_platform_reconciliation_runs_terminal_idle",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_]+$'",
            name="chk_platform_reconciliation_runs_error_code_safe",
        ),
        CheckConstraint("jsonb_typeof(scope_json) = 'object'", name="chk_platform_reconciliation_runs_scope_object"),
        CheckConstraint("jsonb_typeof(watermark_json) = 'object'", name="chk_platform_reconciliation_runs_watermark_object"),
        CheckConstraint(
            "scanned_count >= 0 AND discrepancy_count >= 0 AND resolved_count >= 0 AND failed_count >= 0",
            name="chk_platform_reconciliation_runs_counts_nonnegative",
        ),
        CheckConstraint("status = 'running' OR completed_at IS NOT NULL", name="chk_platform_reconciliation_runs_completed_metadata"),
        Index("ix_platform_reconciliation_runs_status", "provider_code", "status", "started_at"),
        Index(
            "ix_platform_reconciliation_runs_claim_recovery",
            "status",
            "claim_state",
            "claim_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )


class PlatformReconciliationItem(Base):
    __tablename__ = "platform_reconciliation_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_reconciliation_runs.id", ondelete="RESTRICT"), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    provider_object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_object_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    local_object_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    local_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    discrepancy_classification: Mapped[str] = mapped_column(String(80), nullable=False)
    resolution_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    claim_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    evidence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    run: Mapped[PlatformReconciliationRun] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint(
            "reconciliation_run_id",
            "provider_object_type",
            "external_object_ref",
            "discrepancy_classification",
            name="uq_platform_reconciliation_items_run_discrepancy",
        ),
        CheckConstraint("btrim(provider_object_type) <> ''", name="chk_platform_reconciliation_items_provider_object_type"),
        CheckConstraint("btrim(external_object_ref) <> ''", name="chk_platform_reconciliation_items_external_ref"),
        CheckConstraint(
            "(local_object_type IS NULL AND local_object_id IS NULL) OR (local_object_type IS NOT NULL AND btrim(local_object_type) <> '' AND local_object_id IS NOT NULL)",
            name="chk_platform_reconciliation_items_local_shape",
        ),
        CheckConstraint("btrim(discrepancy_classification) <> ''", name="chk_platform_reconciliation_items_discrepancy"),
        CheckConstraint("resolution_status IN ('open', 'resolved', 'ignored', 'failed')", name="chk_platform_reconciliation_items_resolution_status"),
        CheckConstraint("claim_state IN ('idle', 'processing')", name="chk_platform_reconciliation_items_claim_state"),
        CheckConstraint("attempt_count >= 0", name="chk_platform_reconciliation_items_attempt_count"),
        CheckConstraint(
            "(claimed_at IS NULL AND claim_expires_at IS NULL) OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="chk_platform_reconciliation_items_claim_timestamps_paired",
        ),
        CheckConstraint(
            "claim_state <> 'processing' OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="chk_platform_reconciliation_items_processing_claim_metadata",
        ),
        CheckConstraint(
            "claim_state <> 'idle' OR (claimed_at IS NULL AND claim_expires_at IS NULL)",
            name="chk_platform_reconciliation_items_idle_claim_metadata",
        ),
        CheckConstraint(
            "claim_expires_at IS NULL OR claim_expires_at > claimed_at",
            name="chk_platform_reconciliation_items_positive_lease",
        ),
        CheckConstraint(
            "resolution_status NOT IN ('resolved', 'ignored', 'failed') OR claim_state = 'idle'",
            name="chk_platform_reconciliation_items_terminal_idle",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_]+$'",
            name="chk_platform_reconciliation_items_error_code_safe",
        ),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_reconciliation_items_evidence_hash"),
        CheckConstraint("btrim(evidence_ref) <> ''", name="chk_platform_reconciliation_items_evidence_ref"),
        CheckConstraint(
            "resolution_status NOT IN ('resolved', 'ignored', 'failed') OR resolved_at IS NOT NULL",
            name="chk_platform_reconciliation_items_resolved_metadata",
        ),
        Index("ix_platform_reconciliation_items_org_status", "organization_id", "resolution_status", postgresql_where=text("organization_id IS NOT NULL")),
        Index("ix_platform_reconciliation_items_external", "provider_object_type", "external_object_ref"),
        Index(
            "ix_platform_reconciliation_items_claimable",
            "reconciliation_run_id",
            "created_at",
            postgresql_where=text("resolution_status = 'open' AND claim_state = 'idle'"),
        ),
        Index(
            "ix_platform_reconciliation_items_stale_processing",
            "claim_expires_at",
            postgresql_where=text("resolution_status = 'open' AND claim_state = 'processing'"),
        ),
        Index("ix_platform_reconciliation_items_run_resolution", "reconciliation_run_id", "resolution_status"),
    )
