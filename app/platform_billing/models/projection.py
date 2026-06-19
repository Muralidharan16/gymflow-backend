"""
app/platform_billing/models/projection.py
==========================================
Platform Billing projection ORM models — Phase 2.

Entitlement, access, and usage projections are derived read-optimized
snapshots. They are replaceable derived data, not financial history.
"""

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
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PlatformEntitlementProjection(Base):
    __tablename__ = "platform_entitlement_projection"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True)
    feature_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_integer: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_override_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    source_subscription_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolution_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    input_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    __table_args__ = (
        CheckConstraint("value_type IN ('boolean', 'integer', 'string', 'json')", name="chk_platform_entitlement_projection_value_type"),
        CheckConstraint("num_nonnulls(value_boolean, value_integer, value_string, value_json) = 1", name="chk_platform_entitlement_projection_one_value"),
        CheckConstraint("resolution_version > 0", name="chk_platform_entitlement_projection_resolution_version_positive"),
        CheckConstraint("source_subscription_version >= 0", name="chk_platform_entitlement_projection_source_version_nonneg"),
        CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_entitlement_projection_input_hash"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="chk_platform_entitlement_projection_effective_until"),
        ForeignKeyConstraint(
            ["source_override_id", "organization_id"],
            ["platform_access_overrides.id", "platform_access_overrides.organization_id"],
            name="fk_platform_entitlement_projection_override_org",
            ondelete="RESTRICT",
        ),
        Index("ix_platform_entitlement_projection_org", "organization_id"),
    )


class PlatformAccessProjection(Base):
    __tablename__ = "platform_access_projection"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_detail_safe: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    next_transition_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    recovery_actions_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    source_subscription_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    input_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    __table_args__ = (
        CheckConstraint("mode IN ('full', 'limited_write', 'read_only', 'billing_only', 'blocked')", name="chk_platform_access_projection_mode"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_access_projection_reason_code_nonempty"),
        CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="chk_platform_access_projection_input_hash"),
        CheckConstraint("resolution_version > 0", name="chk_platform_access_projection_resolution_version_positive"),
        CheckConstraint("source_subscription_version IS NULL OR source_subscription_version >= 0", name="chk_platform_access_projection_source_version_nonneg"),
        CheckConstraint("next_transition_at IS NULL OR next_transition_at > effective_from", name="chk_platform_access_projection_next_transition"),
        CheckConstraint("jsonb_typeof(recovery_actions_json) = 'array'", name="chk_platform_access_projection_recovery_actions_array"),
        CheckConstraint(
            "recovery_actions_json <@ '[\"VIEW_PLAN_BILLING\", \"UPDATE_PAYMENT_METHOD\", \"COMPLETE_PAYMENT_ACTION\", \"DOWNLOAD_INVOICES\", \"CONTACT_SUPPORT\", \"EXPORT_DATA\", \"UNDO_CANCELLATION\"]'::jsonb",
            name="chk_platform_access_projection_recovery_actions_registered",
        ),
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            name="fk_platform_access_projection_subscription_org",
            ondelete="RESTRICT",
        ),
        Index("ix_platform_access_projection_mode", "mode"),
    )


class PlatformUsageProjection(Base):
    __tablename__ = "platform_usage_projection"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True)
    metric_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    current_value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    measured_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    source_high_watermark: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stale_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint("current_value >= 0", name="chk_platform_usage_projection_current_value_nonneg"),
        CheckConstraint("btrim(metric_key) <> ''", name="chk_platform_usage_projection_metric_key_nonempty"),
        CheckConstraint("stale_after >= measured_at", name="chk_platform_usage_projection_stale_after_future"),
        Index("ix_platform_usage_projection_org", "organization_id"),
    )
