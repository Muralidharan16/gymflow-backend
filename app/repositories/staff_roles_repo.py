from typing import List, Optional
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_user import OrganizationUser, BranchStaffRole, BranchStaffRoleEnum

class StaffRolesRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_user(self, user: OrganizationUser) -> OrganizationUser:
        self.session.add(user)
        await self.session.flush()
        return user

    async def get_or_create_member(self, org_id: UUID, user_id: UUID) -> "OrganizationMember":
        from app.models.organization_user import OrganizationMember
        query = select(OrganizationMember).where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.deleted_at.is_(None)
        )
        result = await self.session.execute(query)
        member = result.scalar_one_or_none()
        
        if not member:
            member = OrganizationMember(
                org_id=org_id,
                user_id=user_id,
                membership_status_id=3 # active
            )
            self.session.add(member)
            await self.session.flush()
            
        return member

    async def get_user_by_id(self, user_id: UUID, org_id: UUID) -> Optional[OrganizationUser]:
        query = (
            select(OrganizationUser)
            .where(
                OrganizationUser.id == user_id,
                OrganizationUser.org_id == org_id,
                OrganizationUser.deleted_at.is_(None)
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_user_by_email(self, email: str, org_id: UUID) -> Optional[OrganizationUser]:
        query = (
            select(OrganizationUser)
            .where(
                OrganizationUser.email == email,
                OrganizationUser.org_id == org_id,
                OrganizationUser.deleted_at.is_(None)
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_users(self, org_id: UUID, skip: int = 0, limit: int = 100) -> List[OrganizationUser]:
        query = (
            select(OrganizationUser)
            .where(
                OrganizationUser.org_id == org_id,
                OrganizationUser.deleted_at.is_(None)
            )
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def create_role_assignment(self, assignment: BranchStaffRole) -> BranchStaffRole:
        self.session.add(assignment)
        await self.session.flush()
        return assignment

    async def get_role_assignment_by_id(self, assignment_id: UUID, org_id: UUID) -> Optional[BranchStaffRole]:
        query = (
            select(BranchStaffRole)
            .options(selectinload(BranchStaffRole.member))
            .where(
                BranchStaffRole.id == assignment_id,
                BranchStaffRole.org_id == org_id,
                BranchStaffRole.deleted_at.is_(None)
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_active_assignments(
        self,
        branch_id: UUID,
        member_id: UUID,
        role_id: int,
        org_id: UUID
    ) -> List[BranchStaffRole]:
        """
        Retrieves active assignments to inspect overlapping ranges.
        """
        now = datetime.now(timezone.utc)
        query = (
            select(BranchStaffRole)
            .where(
                BranchStaffRole.branch_id == branch_id,
                BranchStaffRole.organization_member_id == member_id,
                BranchStaffRole.role_id == role_id,
                BranchStaffRole.org_id == org_id,
                BranchStaffRole.deleted_at.is_(None),
                BranchStaffRole.revoked_at.is_(None)
            )
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_branch_staff_roles(
        self,
        branch_id: UUID,
        org_id: UUID,
        include_inactive: bool = False
    ) -> List[BranchStaffRole]:
        query = (
            select(BranchStaffRole)
            .options(selectinload(BranchStaffRole.member))
            .where(
                BranchStaffRole.branch_id == branch_id,
                BranchStaffRole.org_id == org_id,
                BranchStaffRole.deleted_at.is_(None)
            )
        )
        if not include_inactive:
            now = datetime.now(timezone.utc)
            query = query.where(
                BranchStaffRole.revoked_at.is_(None),
                BranchStaffRole.effective_from <= now,
                or_(BranchStaffRole.effective_to.is_(None), BranchStaffRole.effective_to > now)
            )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_user_staff_roles(
        self,
        user_id: UUID,
        org_id: UUID,
        include_inactive: bool = False
    ) -> List[BranchStaffRole]:
        from app.models.organization_user import OrganizationMember
        query = (
            select(BranchStaffRole)
            .join(BranchStaffRole.member)
            .options(selectinload(BranchStaffRole.member))
            .where(
                OrganizationMember.user_id == user_id,
                BranchStaffRole.org_id == org_id,
                BranchStaffRole.deleted_at.is_(None)
            )
        )
        if not include_inactive:
            now = datetime.now(timezone.utc)
            query = query.where(
                BranchStaffRole.revoked_at.is_(None),
                BranchStaffRole.effective_from <= now,
                or_(BranchStaffRole.effective_to.is_(None), BranchStaffRole.effective_to > now)
            )
        result = await self.session.execute(query)
        return list(result.scalars().all())
