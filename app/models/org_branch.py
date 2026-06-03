import uuid
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import String, Boolean, ForeignKey, Index, UniqueConstraint, BigInteger, CheckConstraint, text, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, CITEXT, TSVECTOR
import sqlalchemy as sa

from app.models.base import Base, TimestampMixin, new_uuid


class AllowedBranchTransition(Base):
    __tablename__ = "allowed_branch_transitions"

    from_status: Mapped[str] = mapped_column(String, primary_key=True)
    to_status: Mapped[str] = mapped_column(String, primary_key=True)


class OrgBranch(Base, TimestampMixin):
    __tablename__ = "org_branches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    
    branch_name: Mapped[str] = mapped_column(String(120), nullable=False)
    branch_code: Mapped[str] = mapped_column(String(50), nullable=False)
    internal_slug: Mapped[str] = mapped_column(CITEXT, nullable=False)
    
    search_normalized_name: Mapped[Optional[str]] = mapped_column(
        String, 
        sa.Computed("lower(regexp_replace(branch_name, '\\s+', ' ', 'g'))", persisted=True)
    )
    
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"), default="UTC", nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"), default="USD", nullable=False)
    region_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    
    address_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True
    )

    state: Mapped["OrgBranchState"] = relationship(
        "OrgBranchState",
        back_populates="branch",
        uselist=False,
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_org_branch_pair"),
        UniqueConstraint("org_id", "branch_code", name="uq_branch_code_per_org"),
        Index("ix_org_branches_org_id_v2", "org_id"),
        Index("ix_org_branches_name_trgm", "branch_name", postgresql_using="gin", postgresql_ops={"branch_name": "gin_trgm_ops"}),
        Index("ix_org_branches_normalized", "search_normalized_name", postgresql_using="gin", postgresql_ops={"search_normalized_name": "gin_trgm_ops"}),
    )

class BranchNameTranslation(Base):
    __tablename__ = "branch_name_translations"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="CASCADE"), primary_key=True)
    locale: Mapped[str] = mapped_column(String(10), primary_key=True)
    branch_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    
    search_vector: Mapped[Optional[Any]] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('simple', branch_name)", persisted=True)
    )

    __table_args__ = (
        Index("ix_branch_translations_search", "search_vector", postgresql_using="gin"),
    )


class OrgBranchState(Base):
    __tablename__ = "org_branch_state"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False
    )
    
    branch_status: Mapped[str] = mapped_column(
        String(30), server_default=text("'active'"), default="active", nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True, nullable=False)
    
    # Branch Lifecycle Control Plane (v18.0) additions
    status: Mapped[str] = mapped_column(String, server_default=text("'active'"), default="active", nullable=False)
    is_operational: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True, nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    status_changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    status_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    transition_source: Mapped[str] = mapped_column(String(50), server_default=text("'api'"), default="api", nullable=False)
    scheduled_transition_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    scheduled_transition_to: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    lifecycle_transition_in_progress: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), default=False, nullable=False)
    saga_last_checkpoint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    saga_compensation_strategy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    watchdog_recovered_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    watchdog_recovery_count: Mapped[int] = mapped_column(default=0, nullable=False)
    search_visibility_version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    search_last_synced_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    search_sync_failed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reconciliation_claimed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    reconciliation_claimed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    worm_archive_uri: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worm_archive_checksum: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worm_archive_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    worm_archive_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"), default=1, nullable=False)
    search_logical_clock: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0, nullable=False)
    search_epoch_ulid: Mapped[str] = mapped_column(String(26), nullable=False)
    
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )

    branch: Mapped["OrgBranch"] = relationship("OrgBranch", back_populates="state")

    __table_args__ = (
        ForeignKeyConstraint(
            ["branch_id", "org_id"],
            ["org_branches.id", "org_branches.org_id"],
            name="fk_branch_state_org"
        ),
        CheckConstraint(
            "branch_status IN ('active', 'inactive', 'suspended', 'under_renovation', 'pending_deletion', 'archived', 'cleanup_failed')",
            name="chk_valid_branch_status"
        ),
    )





class ActiveOrgBranch(Base):
    __tablename__ = "v_active_org_branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    branch_name: Mapped[str] = mapped_column(String)
    branch_code: Mapped[str] = mapped_column(String)
    internal_slug: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    currency_code: Mapped[str] = mapped_column(String)
    region_code: Mapped[Optional[str]] = mapped_column(String)
    country_code: Mapped[Optional[str]] = mapped_column(String)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    
    branch_status: Mapped[str] = mapped_column(String)
    is_primary: Mapped[bool] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean)
    is_public: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(BigInteger)
    state_updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))

