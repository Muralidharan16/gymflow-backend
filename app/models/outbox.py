import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from sqlalchemy import String, SmallInteger, TIMESTAMP, text, CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.models.base import Base


class TransactionalOutbox(Base):
    """Durable internal queue for branch-hours projection work.

    API code must not insert this model directly.  Producers enqueue through the
    database-owned branch-hours enqueue functions so tenant/event shape checks
    remain inside the same transaction as the schedule mutation.  Worker code
    claims rows with a dedicated database identity.
    """

    __tablename__ = "transactional_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("org_branches.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("1"), default=1, nullable=False
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("clock_timestamp()"),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    parent_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactional_outbox.id", ondelete="RESTRICT"),
        nullable=True,
    )

    delivery_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0"), default=0, nullable=False
    )
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    processed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    dead_lettered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("clock_timestamp()"),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    leased_until: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    leased_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "delivery_attempts >= 0 AND delivery_attempts <= 15",
            name="chk_outbox_attempt_bounds_model",
        ),
    )
