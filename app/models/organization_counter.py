import uuid
from datetime import datetime
from sqlalchemy import String, UUID, TIMESTAMP, Integer, text, UniqueConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class OrganizationCounter(Base):
    __tablename__ = "organization_counters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    counter_key: Mapped[str] = mapped_column(String(50), nullable=False)
    current_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), onupdate=text("now()"), nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "counter_key", name="uix_org_counter_key"),
    )
