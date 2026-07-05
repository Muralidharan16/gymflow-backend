import uuid
import enum
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Boolean, ForeignKey, Index, text, ForeignKeyConstraint, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, CITEXT

from app.models.base import Base, TimestampMixin, new_uuid

class BranchStaffRoleEnum(str, enum.Enum):
    manager = "manager"
    trainer = "trainer"
    receptionist = "receptionist"
    auditor = "auditor"

class OrganizationUser(Base, TimestampMixin):
    __tablename__ = "organization_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    token_version: Mapped[int] = mapped_column(default=1, server_default=text("1"), nullable=False)
    
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        Index("ix_org_users_email_lower_active", "email", unique=True, postgresql_where=text("deleted_at IS NULL")),
        UniqueConstraint("id", "org_id", name="uq_org_users_pair"),
    )

class OrganizationMember(Base, TimestampMixin):
    __tablename__ = "organization_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    membership_status_id: Mapped[int] = mapped_column(nullable=False, default=1)
    permission_version: Mapped[int] = mapped_column(default=1, nullable=False)
    region_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    user: Mapped["OrganizationUser"] = relationship("OrganizationUser", foreign_keys=[user_id])
    
    roles: Mapped[List["BranchStaffRole"]] = relationship(
        "BranchStaffRole",
        back_populates="member",
        foreign_keys="[BranchStaffRole.organization_member_id]",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_member_user"),
        UniqueConstraint("id", "org_id", name="uq_org_member_pair"),
        ForeignKeyConstraint(["user_id", "org_id"], ["organization_users.id", "organization_users.org_id"], name="fk_org_members_user_org", ondelete="RESTRICT"),
    )

class BranchStaffRole(Base):
    __tablename__ = "branch_staff_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # New v18.0 fields
    organization_member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role_id: Mapped[int] = mapped_column(nullable=False)
    scope_type_id: Mapped[int] = mapped_column(nullable=False, default=2)
    assignment_source: Mapped[str] = mapped_column(String(32), default='dashboard', server_default=text("'dashboard'"), nullable=False)

    assigned_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=datetime.utcnow, server_default=text("clock_timestamp()"), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    member: Mapped["OrganizationMember"] = relationship(
        "OrganizationMember",
        back_populates="roles",
        foreign_keys=[organization_member_id]
    )

    @property
    def user_id(self) -> uuid.UUID:
        # Assumes member is eager loaded
        return self.member.user_id if self.member else None

    @property
    def role(self) -> BranchStaffRoleEnum:
        ROLE_MAP_REV = {
            3: BranchStaffRoleEnum.manager,
            4: BranchStaffRoleEnum.trainer,
            5: BranchStaffRoleEnum.receptionist,
            6: BranchStaffRoleEnum.auditor
        }
        return ROLE_MAP_REV.get(self.role_id, BranchStaffRoleEnum.trainer)

    __table_args__ = (
        ForeignKeyConstraint(["branch_id", "org_id"], ["org_branches.id", "org_branches.org_id"], name="fk_branch_staff_branch_org", ondelete="RESTRICT"),
        ForeignKeyConstraint(["assigned_by"], ["organization_members.id"], name="fk_bsr_assigned_by", ondelete="RESTRICT"),
        ForeignKeyConstraint(["revoked_by"], ["organization_members.id"], name="fk_bsr_revoked_by", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_member_id", "org_id"], ["organization_members.id", "organization_members.org_id"], name="fk_bsr_member_org", ondelete="RESTRICT"),
    )
