from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_uuid


class PlatformBillingAuditEvent(Base):
    __tablename__ = "platform_billing_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    before_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    metadata_redacted_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'system', 'provider', 'support')", name="chk_platform_billing_audit_events_actor_type"),
        CheckConstraint("outcome IN ('succeeded', 'failed', 'denied', 'noop')", name="chk_platform_billing_audit_events_outcome"),
        UniqueConstraint("id", "organization_id", name="uq_platform_billing_audit_events_id_org"),
        Index("ix_platform_billing_audit_events_org_recorded", "organization_id", text("recorded_at DESC")),
        Index("ix_platform_billing_audit_events_org_target", "organization_id", "target_type", "target_id"),
    )
