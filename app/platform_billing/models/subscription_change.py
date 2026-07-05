"""
app/platform_billing/models/subscription_change.py
==================================================
PlatformSubscriptionChange ORM model — Phase 2.

Persistence foundation for subscription change requests.
Commands, previews, provider calls, and APIs are not implemented
in Phase 2; only the schema and read support exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CHAR, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformSubscriptionChange(Base):
    __tablename__ = "platform_subscription_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    from_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    from_price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requested_effective_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    actual_effective_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    preview_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    request_idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    expected_subscription_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    canceled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_detail_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_changes_subscription_org",
        ),
        ForeignKeyConstraint(
            ["from_plan_version_id"],
            ["platform_plan_versions.id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_changes_from_plan",
        ),
        ForeignKeyConstraint(
            ["to_plan_version_id"],
            ["platform_plan_versions.id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_changes_to_plan",
        ),
        ForeignKeyConstraint(
            ["from_price_id"],
            ["platform_prices.id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_changes_from_price",
        ),
        ForeignKeyConstraint(
            ["to_price_id"],
            ["platform_prices.id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_changes_to_price",
        ),
        CheckConstraint(
            "change_type IN ('upgrade', 'downgrade', 'cancel', 'undo_cancel', 'pause', 'resume', 'reactivate')",
            name="chk_platform_subscription_changes_change_type",
        ),
        CheckConstraint(
            "status IN ('requested', 'validated', 'provider_pending', 'scheduled', 'applied', 'canceled', 'failed_retryable', 'failed_final')",
            name="chk_platform_subscription_changes_status",
        ),
        CheckConstraint("jsonb_typeof(preview_snapshot_json) = 'object'", name="chk_platform_subscription_changes_preview_object"),
        CheckConstraint("btrim(request_idempotency_key) <> ''", name="chk_platform_subscription_changes_idempotency_key_nonempty"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="chk_platform_subscription_changes_request_hash_format"),
        CheckConstraint("expected_subscription_version >= 1", name="chk_platform_subscription_changes_expected_version_positive"),
        CheckConstraint("version >= 1", name="chk_platform_subscription_changes_version_positive"),
        UniqueConstraint("id", "organization_id", name="uq_platform_subscription_changes_id_org"),
        UniqueConstraint("organization_id", "request_idempotency_key", name="uq_platform_subscription_changes_idempotency_key"),
        Index("ix_platform_subscription_changes_org_subscription", "organization_id", "subscription_id"),
        Index("ix_platform_subscription_changes_org_status", "organization_id", "status"),
    )