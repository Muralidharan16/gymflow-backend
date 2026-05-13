# app/models/audit.py
import uuid
from datetime import datetime
from sqlalchemy import String, ForeignKey, text, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, INET, JSONB
from typing import Optional
from app.models.base import Base, new_uuid

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("owners.id", ondelete="SET NULL"),
        nullable=True,
    )
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), default=dict, nullable=False
    )
    
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_audit_user_created", "user_id", text("created_at DESC")),
        Index("ix_audit_org_action", "organization_id", "action"),
    )
