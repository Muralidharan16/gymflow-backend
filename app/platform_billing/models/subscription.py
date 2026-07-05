from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.types import CHAR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_uuid


class PlatformSubscription(Base):
    __tablename__ = "platform_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=False)
    current_price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    cancellation_effective_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    provider_subscription_mapping_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    items: Mapped[list["PlatformSubscriptionItem"]] = relationship(back_populates="subscription")
    periods: Mapped[list["PlatformSubscriptionPeriod"]] = relationship(back_populates="subscription")
    events: Mapped[list["PlatformSubscriptionEvent"]] = relationship(back_populates="subscription")

    __table_args__ = (
        ForeignKeyConstraint(
            ["billing_account_id", "organization_id"],
            ["platform_billing_accounts.id", "platform_billing_accounts.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_subscriptions_billing_account_org",
        ),
        CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'pause_scheduled', 'paused', 'cancel_scheduled', 'canceled', 'expired')",
            name="chk_platform_subscriptions_status",
        ),
        CheckConstraint("current_period_end > current_period_start", name="chk_platform_subscriptions_period_order"),
        CheckConstraint("version >= 1", name="chk_platform_subscriptions_version_positive"),
        UniqueConstraint("id", "organization_id", name="uq_platform_subscriptions_id_org"),
        Index("ix_platform_subscriptions_org_status", "organization_id", "status"),
        Index(
            "ux_platform_subscriptions_one_current_per_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('trialing', 'active', 'past_due', 'pause_scheduled', 'paused', 'cancel_scheduled')"),
        ),
    )


class PlatformSubscriptionItem(Base):
    __tablename__ = "platform_subscription_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_plan_versions.id", ondelete="RESTRICT"), nullable=False)
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("platform_prices.id", ondelete="RESTRICT"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scheduled'"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    subscription: Mapped[PlatformSubscription] = relationship(back_populates="items")

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_items_subscription_org",
        ),
        CheckConstraint("item_type IN ('base_plan', 'addon')", name="chk_platform_subscription_items_item_type"),
        CheckConstraint("quantity > 0", name="chk_platform_subscription_items_quantity_positive"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="chk_platform_subscription_items_effective_order"),
        CheckConstraint("status IN ('scheduled', 'active', 'ended')", name="chk_platform_subscription_items_status"),
        CheckConstraint("version >= 1", name="chk_platform_subscription_items_version_positive"),
        UniqueConstraint("id", "organization_id", name="uq_platform_subscription_items_id_org"),
        Index("ix_platform_subscription_items_org_subscription", "organization_id", "subscription_id"),
        Index(
            "ux_platform_subscription_items_one_active_base_plan",
            "subscription_id",
            unique=True,
            postgresql_where=text("item_type = 'base_plan' AND status = 'active'"),
        ),
    )


class PlatformSubscriptionPeriod(Base):
    __tablename__ = "platform_subscription_periods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    period_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scheduled'"))
    starts_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    source_invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_change_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_override_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))

    subscription: Mapped[PlatformSubscription] = relationship(back_populates="periods")

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_periods_subscription_org",
        ),
        CheckConstraint(
            "period_type IN ('trial', 'paid', 'grace', 'extension', 'post_cancel_read_only')",
            name="chk_platform_subscription_periods_period_type",
        ),
        CheckConstraint("status IN ('scheduled', 'open', 'closed', 'void')", name="chk_platform_subscription_periods_status"),
        CheckConstraint("ends_at > starts_at", name="chk_platform_subscription_periods_order"),
        UniqueConstraint("id", "organization_id", name="uq_platform_subscription_periods_id_org"),
        Index("ix_platform_subscription_periods_org_subscription", "organization_id", "subscription_id"),
    )


class PlatformSubscriptionEvent(Base):
    __tablename__ = "platform_subscription_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("clock_timestamp()"))
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    subscription: Mapped[PlatformSubscription] = relationship(back_populates="events")

    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "organization_id"],
            ["platform_subscriptions.id", "platform_subscriptions.organization_id"],
            ondelete="RESTRICT",
            name="fk_platform_subscription_events_subscription_org",
        ),
        CheckConstraint("sequence_number > 0", name="chk_platform_subscription_events_sequence_positive"),
        CheckConstraint("actor_type IN ('user', 'system', 'provider', 'support')", name="chk_platform_subscription_events_actor_type"),
        CheckConstraint(
            "source_type IN ('command', 'webhook', 'reconciliation', 'scheduler', 'migration')",
            name="chk_platform_subscription_events_source_type",
        ),
        UniqueConstraint("id", "organization_id", name="uq_platform_subscription_events_id_org"),
        UniqueConstraint("subscription_id", "sequence_number", name="uq_platform_subscription_events_subscription_sequence"),
        Index("ix_platform_subscription_events_org_subscription", "organization_id", "subscription_id", "sequence_number"),
        Index(
            "ux_platform_subscription_events_source_identity",
            "subscription_id",
            "source_type",
            "source_id",
            "event_type",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        Index(
            "ux_platform_subscription_events_evidence_identity",
            "subscription_id",
            "evidence_sha256",
            "event_type",
            unique=True,
            postgresql_where=text("evidence_sha256 IS NOT NULL"),
        ),
    )
