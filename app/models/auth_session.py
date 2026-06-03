import uuid
from datetime import datetime
from typing import Optional, Any, Dict
from sqlalchemy import String, Boolean, ForeignKey, Index, text, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, INET

from app.models.base import Base, TimestampMixin, new_uuid

class AuthSessionFamily(Base):
    __tablename__ = "auth_session_families"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)

    # Relationships
    sessions: Mapped[list["AuthSession"]] = relationship("AuthSession", back_populates="family", cascade="all, delete-orphan")

class AuthSession(Base):
    __tablename__ = "auth_sessions"
    
    # We map this for SQLAlchemy metadata but note it's partitioned in postgres
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    # The actual primary key in PostgreSQL includes created_at because of partitioning
    # SQLAlchemy might complain about missing created_at in PK, but since created_at is in TimestampMixin,
    # we can define __mapper_args__ or just leave it since the DB handles the constraint.
    
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    
    token_family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("auth_session_families.id", ondelete="CASCADE"), nullable=False)
    
    parent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    replaced_by_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_info: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)
    
    token_version_snapshot: Mapped[int] = mapped_column(nullable=False)
    
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    reuse_detected_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    compromised_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    risk_score: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    last_geo: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)

    family: Mapped["AuthSessionFamily"] = relationship("AuthSessionFamily", back_populates="sessions")

    __table_args__ = (
        Index("uq_session_replacement", "replaced_by_session_id", unique=True, postgresql_where=text("replaced_by_session_id IS NOT NULL")),
        # NOTE: SQLAlchemy doesn't support RANGE partitions natively in Base declarative with standard PK definition easily.
        # We will handle the partitioning DDL in Alembic.
    )
