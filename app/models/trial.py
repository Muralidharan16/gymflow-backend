# app/models/trial.py
import uuid
from datetime import datetime
from sqlalchemy import String, ForeignKey, text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from typing import Optional
from app.models.base import Base, TimestampMixin, new_uuid

class TrialSubscription(Base, TimestampMixin):
    __tablename__ = "trial_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    
    trial_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    trial_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    grace_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    hard_lock_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    
    # status: active, soft_locked, hard_locked, converted
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"), default="active", nullable=False)
    
    soft_locked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    hard_locked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    converted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization", backref="trial_sub")

    __table_args__ = (
        Index("ix_trial_status_end", "status", "trial_end"),
    )
