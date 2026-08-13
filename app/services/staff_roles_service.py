import json
import logging
from uuid import UUID
from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.core.security import hash_password
from app.core.redis import redis_client
from app.models.organization_user import OrganizationUser, BranchStaffRole, BranchStaffRoleEnum
from app.repositories.staff_roles_repo import StaffRolesRepository
from app.repositories.branch_repo import BranchRepository
from app.schemas.staff_roles import (
    OrganizationUserCreate,
    OrganizationUserUpdate,
    BranchStaffRoleCreate
)

logger = logging.getLogger(__name__)

_BRANCH_ROLE_OVERLAP_SQLSTATE = "23P01"
_BRANCH_ROLE_OVERLAP_CONSTRAINT = "ex_branch_role_overlap_v2"


def _integrity_error_metadata(exc: IntegrityError) -> tuple[Optional[str], Optional[str]]:
    """Extract PostgreSQL SQLSTATE and constraint name across DBAPI adapters."""
    sqlstate: Optional[str] = None
    constraint_name: Optional[str] = None
    candidates = [
        getattr(exc, "orig", None),
        getattr(getattr(exc, "orig", None), "__cause__", None),
        getattr(exc, "__cause__", None),
    ]

    for candidate in candidates:
        if candidate is None:
            continue
        if sqlstate is None:
            sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if constraint_name is None:
            constraint_name = getattr(candidate, "constraint_name", None)
            diag = getattr(candidate, "diag", None)
            if constraint_name is None and diag is not None:
                constraint_name = getattr(diag, "constraint_name", None)

    return sqlstate, constraint_name


def _is_branch_role_overlap_violation(exc: IntegrityError) -> bool:
    sqlstate, constraint_name = _integrity_error_metadata(exc)
    return (
        sqlstate == _BRANCH_ROLE_OVERLAP_SQLSTATE
        and constraint_name == _BRANCH_ROLE_OVERLAP_CONSTRAINT
    )


class StaffRolesService:
    def __init__(self, session):
        self.session = session
        self.repo = StaffRolesRepository(session)
        self.branch_repo = BranchRepository(session)

    def _get_cache_key(self, user_id: UUID) -> str:
        return f"user:branch_roles:{user_id}"

    async def invalidate_user_role_cache(self, user_id: UUID) -> None:
        cache_key = self._get_cache_key(user_id)
        try:
            await redis_client.delete(cache_key)
        except Exception:
            logger.exception("Failed to delete branch roles cache from Redis")

    async def get_user_branch_roles(self, user_id: UUID, org_id: UUID) -> Dict[str, List[str]]:
        """
        Fetches active branch roles for a user, checking Redis cache first, falling back to PostgreSQL.
        """
        cache_key = self._get_cache_key(user_id)
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            logger.exception("Redis read failure during get_user_branch_roles")

        # Fallback to Database
        db_roles = await self.repo.list_user_staff_roles(user_id, org_id, include_inactive=False)

        branch_roles = {}
        for r in db_roles:
            b_str = str(r.branch_id)
            if b_str not in branch_roles:
                branch_roles[b_str] = []
            branch_roles[b_str].append(r.role.value)

        # Set cache
        try:
            await redis_client.set(cache_key, json.dumps(branch_roles), ex=3600)
        except Exception:
            logger.exception("Redis write failure during get_user_branch_roles")

        return branch_roles

    async def create_organization_user(self, data: OrganizationUserCreate, org_id: UUID) -> OrganizationUser:
        # Check if email is already registered in the organization
        existing = await self.repo.get_user_by_email(data.email, org_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this email is already registered in this organization."
            )

        hashed_pw = hash_password(data.password)
        new_user = OrganizationUser(
            org_id=org_id,
            name=data.name,
            email=data.email.lower(),
            password_hash=hashed_pw,
            phone=data.phone,
            is_active=data.is_active,
            is_verified=False
        )
        return await self.repo.create_user(new_user)

    async def get_organization_user(self, user_id: UUID, org_id: UUID) -> OrganizationUser:
        user = await self.repo.get_user_by_id(user_id, org_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization user not found."
            )
        return user

    async def list_organization_users(self, org_id: UUID, skip: int = 0, limit: int = 100) -> List[OrganizationUser]:
        return await self.repo.list_users(org_id, skip, limit)

    async def update_organization_user(
        self,
        user_id: UUID,
        org_id: UUID,
        data: OrganizationUserUpdate
    ) -> OrganizationUser:
        user = await self.repo.get_user_by_id(user_id, org_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization user not found."
            )

        if data.name is not None:
            user.name = data.name
        if data.phone is not None:
            user.phone = data.phone
        if data.is_active is not None:
            user.is_active = data.is_active
            if data.is_active is False:
                # Trigger user_version increment to force logout of deactivated user
                user.token_version += 1
                await self.invalidate_user_role_cache(user_id)

        self.session.add(user)
        await self.session.flush()
        return user

    async def assign_branch_staff_role(
        self,
        branch_id: UUID,
        org_id: UUID,
        assigned_by: UUID,
        data: BranchStaffRoleCreate
    ) -> BranchStaffRole:
        # 1. Validate branch exists and belongs to the org
        branch = await self.branch_repo.get_active_by_id(branch_id, org_id)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Active branch not found."
            )

        # 2. Validate user exists and belongs to the org
        user = await self.repo.get_user_by_id(data.user_id, org_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target user must belong to the same organization."
            )

        # 3. Map role enum to role_id and get/create organization member
        ROLE_MAP = {
            BranchStaffRoleEnum.manager: 3,
            BranchStaffRoleEnum.trainer: 4,
            BranchStaffRoleEnum.receptionist: 5,
            BranchStaffRoleEnum.auditor: 6
        }
        mapped_role_id = ROLE_MAP.get(data.role, 3)
        member = await self.repo.get_or_create_member(org_id, data.user_id)

        # 3.5 Fast-path overlap check for a clear client error. The database
        # exclusion constraint remains authoritative for concurrent writers.
        active_assignments = await self.repo.get_active_assignments(
            branch_id=branch_id,
            member_id=member.id,
            role_id=mapped_role_id,
            org_id=org_id
        )

        for assign in active_assignments:
            e_from_1 = data.effective_from.astimezone(timezone.utc)
            e_to_1 = (
                data.effective_to.astimezone(timezone.utc)
                if data.effective_to
                else datetime.max.replace(tzinfo=timezone.utc)
            )
            e_from_2 = assign.effective_from.astimezone(timezone.utc)
            e_to_2 = (
                assign.effective_to.astimezone(timezone.utc)
                if assign.effective_to
                else datetime.max.replace(tzinfo=timezone.utc)
            )

            if e_from_1 < e_to_2 and e_from_2 < e_to_1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"This user already has an active or scheduled role assignment as '{data.role.value}' during this period."
                )

        # 3.8 Get assigner member
        assigner = await self.repo.get_or_create_member(org_id, assigned_by) if assigned_by else None

        # 4. Create assignment. Handle only the authoritative overlap
        # constraint; every other integrity failure must propagate unchanged.
        new_assignment = BranchStaffRole(
            org_id=org_id,
            branch_id=branch_id,
            organization_member_id=member.id,
            role_id=mapped_role_id,
            scope_type_id=2, # branch scope
            assignment_source='dashboard',
            assigned_by=assigner.id if assigner else None,
            effective_from=data.effective_from,
            effective_to=data.effective_to,
            metadata_=data.metadata,
            member=member
        )

        try:
            created_role = await self.repo.create_role_assignment(new_assignment)
        except IntegrityError as exc:
            if not _is_branch_role_overlap_violation(exc):
                raise
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This user already has an active or scheduled role assignment "
                    f"as '{data.role.value}' during this period."
                ),
            ) from exc

        # 5. Clear Redis Cache for user
        await self.invalidate_user_role_cache(data.user_id)

        return created_role

    async def revoke_branch_staff_role(
        self,
        branch_id: UUID,
        assignment_id: UUID,
        org_id: UUID,
        revoked_by: UUID
    ) -> BranchStaffRole:
        assignment = await self.repo.get_role_assignment_by_id(assignment_id, org_id)
        if not assignment or assignment.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch staff role assignment not found."
            )

        if assignment.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This role assignment has already been revoked."
            )

        # 1. Get revoker member and update assignment
        revoker = await self.repo.get_or_create_member(org_id, revoked_by) if revoked_by else None

        assignment.revoked_at = datetime.now(timezone.utc)
        assignment.revoked_by = revoker.id if revoker else None

        self.session.add(assignment)
        await self.session.flush()

        # 2. Increment user token version to force logout/token rotation
        user = await self.repo.get_user_by_id(assignment.user_id, org_id)
        if user:
            user.token_version += 1
            self.session.add(user)
            await self.session.flush()

        # 3. Clear Redis Cache
        await self.invalidate_user_role_cache(assignment.user_id)

        return assignment

    async def list_branch_staff(
        self,
        branch_id: UUID,
        org_id: UUID,
        include_inactive: bool = False
    ) -> List[BranchStaffRole]:
        return await self.repo.list_branch_staff_roles(branch_id, org_id, include_inactive)
