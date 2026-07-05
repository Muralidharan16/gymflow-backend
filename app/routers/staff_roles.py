from fastapi import APIRouter, Body, HTTPException, Depends, Header, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import Optional, List
import logging

from app.core.database import get_db as get_db_session
from app.core.deps import get_current_active_staff, require_org_admin, Staff
from app.core.db_retry import managed_db_write, CircuitBreaker
from app.schemas.common import Response, MessageResponse
from app.schemas.staff_roles import (
    OrganizationUserCreate,
    OrganizationUserUpdate,
    OrganizationUserResponse,
    BranchStaffRoleCreate,
    BranchStaffRoleResponse,
    PublicStaffSummary
)
from app.services.staff_roles_service import StaffRolesService
from app.routers.branch_contacts import set_session_context, validate_branch_ownership

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["staff-roles"])

# Circuit breaker for staff role write operations
staff_write_circuit_breaker = CircuitBreaker(
    max_failures=5,
    timeout_seconds=60,
    name="staff_roles_writes"
)

# ==============================================================================
# ORGANIZATION USERS MANAGEMENT
# ==============================================================================

@router.post(
    "/organizations/users",
    response_model=Response[OrganizationUserResponse],
    status_code=201,
    summary="Create a new organization user",
    description="Allows organization admins to register a user identity under the organization."
)
async def create_org_user(
    data: OrganizationUserCreate = Body(...),
    current_staff: Staff = Depends(require_org_admin),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    async with managed_db_write(session, circuit_breaker=staff_write_circuit_breaker):
        await set_session_context(
            session,
            org_id=current_staff.org_id,
            user_id=current_staff.id
        )
        user = await service.create_organization_user(data, current_staff.org_id)
        await session.commit()
        
    return Response(data=OrganizationUserResponse.model_validate(user))

@router.get(
    "/organizations/users",
    response_model=Response[List[OrganizationUserResponse]],
    summary="List all users under the organization"
)
async def list_org_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_staff: Staff = Depends(require_org_admin),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    await set_session_context(session, org_id=current_staff.org_id)
    users = await service.list_organization_users(current_staff.org_id, skip, limit)
    return Response(data=[OrganizationUserResponse.model_validate(u) for u in users])

@router.get(
    "/organizations/users/{user_id}",
    response_model=Response[OrganizationUserResponse],
    summary="Retrieve details of a specific organization user"
)
async def get_org_user(
    user_id: UUID = Path(...),
    current_staff: Staff = Depends(require_org_admin),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    await set_session_context(session, org_id=current_staff.org_id)
    user = await service.get_organization_user(user_id, current_staff.org_id)
    return Response(data=OrganizationUserResponse.model_validate(user))

@router.patch(
    "/organizations/users/{user_id}",
    response_model=Response[OrganizationUserResponse],
    summary="Update organization user details"
)
async def update_org_user(
    user_id: UUID = Path(...),
    data: OrganizationUserUpdate = Body(...),
    current_staff: Staff = Depends(require_org_admin),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    async with managed_db_write(session, circuit_breaker=staff_write_circuit_breaker):
        await set_session_context(
            session,
            org_id=current_staff.org_id,
            user_id=current_staff.id
        )
        user = await service.update_organization_user(user_id, current_staff.org_id, data)
        await session.commit()
        
    return Response(data=OrganizationUserResponse.model_validate(user))

# ==============================================================================
# BRANCH STAFF ROLE ASSIGNMENTS
# ==============================================================================

@router.post(
    "/branches/{branch_id}/staff",
    response_model=Response[BranchStaffRoleResponse],
    status_code=201,
    summary="Assign a role to a staff member in a branch"
)
async def assign_staff_role(
    branch_id: UUID = Path(...),
    data: BranchStaffRoleCreate = Body(...),
    current_staff: Staff = Depends(require_org_admin),
    request_id: UUID = Header(..., alias="X-Request-ID"),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    await validate_branch_ownership(branch_id, current_staff.org_id, session)

    async with managed_db_write(session, circuit_breaker=staff_write_circuit_breaker):
        await set_session_context(
            session,
            org_id=current_staff.org_id,
            user_id=current_staff.id,
            request_id=request_id
        )
        role = await service.assign_branch_staff_role(
            branch_id=branch_id,
            org_id=current_staff.org_id,
            assigned_by=current_staff.id,
            data=data
        )
        await session.commit()

    return Response(data=BranchStaffRoleResponse.model_validate(role))

@router.delete(
    "/branches/{branch_id}/staff/{assignment_id}",
    response_model=Response[BranchStaffRoleResponse],
    summary="Revoke an active staff role assignment in a branch"
)
async def revoke_staff_role(
    branch_id: UUID = Path(...),
    assignment_id: UUID = Path(...),
    current_staff: Staff = Depends(require_org_admin),
    request_id: UUID = Header(..., alias="X-Request-ID"),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    await validate_branch_ownership(branch_id, current_staff.org_id, session)

    async with managed_db_write(session, circuit_breaker=staff_write_circuit_breaker):
        await set_session_context(
            session,
            org_id=current_staff.org_id,
            user_id=current_staff.id,
            request_id=request_id
        )
        role = await service.revoke_branch_staff_role(
            branch_id=branch_id,
            assignment_id=assignment_id,
            org_id=current_staff.org_id,
            revoked_by=current_staff.id
        )
        await session.commit()

    return Response(data=BranchStaffRoleResponse.model_validate(role))

@router.get(
    "/branches/{branch_id}/staff",
    response_model=Response[List[PublicStaffSummary]],
    summary="List all staff roles inside a branch"
)
async def list_branch_staff(
    branch_id: UUID = Path(...),
    include_inactive: bool = Query(False),
    current_staff: Staff = Depends(get_current_active_staff),
    session: AsyncSession = Depends(get_db_session)
):
    service = StaffRolesService(session)
    await validate_branch_ownership(branch_id, current_staff.org_id, session)
    await set_session_context(session, org_id=current_staff.org_id)
    roles = await service.list_branch_staff(branch_id, current_staff.org_id, include_inactive)
    return Response(data=[PublicStaffSummary.model_validate(r) for r in roles])
