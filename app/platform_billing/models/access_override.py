"""
app/platform_billing/models/access_override.py
===============================================
PlatformAccessOverride ORM model — Phase 2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformAccessOverride(Base):
    __tablename__ = "platform_access_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    override_type: Mapped[str] = mapped_column(Text, nullable=False)
    capability_or_feature_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scheduled'"))
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    ticket_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        CheckConstraint("override_type IN ('access_mode', 'entitlement')", name="chk_platform_access_overrides_override_type"),
        CheckConstraint("status IN ('scheduled', 'active', 'expired', 'revoked')", name="chk_platform_access_overrides_status"),
        CheckConstraint("expires_at > starts_at", name="chk_platform_access_overrides_expires_after_start"),
        CheckConstraint("btrim(reason_code) <> ''", name="chk_platform_access_overrides_reason_code_nonempty"),
        CheckConstraint("btrim(reason_detail) <> ''", name="chk_platform_access_overrides_reason_detail_nonempty"),
        CheckConstraint("btrim(ticket_reference) <> ''", name="chk_platform_access_overrides_ticket_nonempty"),
        CheckConstraint("jsonb_typeof(value_json) = 'object'", name="chk_platform_access_overrides_value_object"),
        CheckConstraint(
            """
            (
                override_type = 'access_mode'
                AND capability_or_feature_key IS NULL
                AND value_json ? 'mode'
                AND value_json->>'mode' IN ('full', 'limited_write', 'read_only', 'billing_only', 'blocked')
            )
            OR (
                override_type = 'entitlement'
                AND capability_or_feature_key IS NOT NULL
                AND btrim(capability_or_feature_key) <> ''
                AND value_json ? 'value_type'
                AND value_json ? 'value'
                AND value_json->>'value_type' IN ('boolean', 'integer', 'string', 'json')
            )
            """,
            name="chk_platform_access_overrides_shape",
        ),
        CheckConstraint("expires_at <= starts_at + INTERVAL '30 days'", name="chk_platform_access_overrides_duration_max"),
        CheckConstraint("approved_by IS NOT NULL OR expires_at <= starts_at + INTERVAL '7 days'", name="chk_platform_access_overrides_normal_duration"),
        CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="chk_platform_access_overrides_approval_separation"),
        CheckConstraint("status <> 'revoked' OR (revoked_by IS NOT NULL AND revoked_at IS NOT NULL)", name="chk_platform_access_overrides_revoked_metadata"),
        CheckConstraint("version >= 1", name="chk_platform_access_overrides_version_positive"),
        UniqueConstraint("id", "organization_id", name="uq_platform_access_overrides_id_org"),
        Index("ix_platform_access_overrides_org_status", "organization_id", "status"),
        Index("ix_platform_access_overrides_active_window", "organization_id", "status", "starts_at", "expires_at"),
    )
