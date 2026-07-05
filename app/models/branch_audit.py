import uuid
from datetime import datetime
from typing import Optional, Any
import sqlalchemy
from sqlalchemy import String, Integer, BigInteger, SmallInteger, JSON, TIMESTAMP, text, Computed, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, new_uuid

class BranchAuditLog(Base):
    __tablename__ = "branch_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    
    # Sequence and Identifiers
    audit_sequence: Mapped[int] = mapped_column(BigInteger, default=None, server_default=text("GENERATED ALWAYS AS IDENTITY"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=new_uuid)
    request_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    # Topology
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    region_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    # Actor Context
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    
    # Action & Reason
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    action_category: Mapped[str] = mapped_column(String(32), Computed("split_part(action, '.', 1)", persisted=True), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    diff: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    
    # Cryptographic Chain
    previous_event_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    
    # App Context
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    deployment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    # Partition Key
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True, default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)
    
    __table_args__ = (
        CheckConstraint("previous_event_hash IS NOT NULL OR action = 'system.bootstrap'", name="chk_prev_hash_chain"),
        sqlalchemy.UniqueConstraint("event_id", "created_at"),
        {"postgresql_partition_by": "RANGE (created_at)"}
    )
