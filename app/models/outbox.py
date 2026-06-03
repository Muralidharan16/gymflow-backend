import uuid
from datetime import datetime, timezone
from typing import Optional, Any
from sqlalchemy import String, SmallInteger, TIMESTAMP, text, JSON, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.models.base import Base

class TransactionalOutbox(Base):
    __tablename__ = "transactional_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"), default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    
    delivery_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"), default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
    processed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    dead_lettered_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), default=lambda: datetime.now(timezone.utc), nullable=False)
    
    leased_until: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    leased_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        CheckConstraint("delivery_attempts <= 15", name="chk_outbox_max_attempts"),
    )
