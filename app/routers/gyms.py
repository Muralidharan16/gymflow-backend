from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_org_admin
from app.core.deps import Staff
from app.schemas.common import Response, MessageResponse
from app.schemas.gym import GymResponse, GymCreate, GymUpdate, TaxConfigResponse, TaxConfigCreate
from app.services.gym_service import GymService
from app.core.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/gyms", tags=["Gyms"])


@router.get("", response_model=Response[List[GymResponse]])
async def list_branches(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    List all active branches for the authenticated staff's organization.
    """
    service = GymService(db)
    branches = await service.list_branches(current_staff.org_id)
    return Response(data=[GymResponse.model_validate(b) for b in branches])


@router.post("", response_model=Response[GymResponse])
async def create_branch(
    data: GymCreate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new gym branch under the organization.
    Only org admin can create branches.
    """
    service = GymService(db)
    try:
        branch = await service.create_branch(
            current_staff.org_id,
            data,
            current_staff.id
        )
        await db.commit()
        return Response(data=GymResponse.model_validate(branch))
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.get("/{gym_id}", response_model=Response[GymResponse])
async def get_branch_detail(
    gym_id: UUID,
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get detailed information of a specific branch.
    """
    service = GymService(db)
    try:
        branch = await service.get_branch(gym_id, current_staff.org_id)
        return Response(data=GymResponse.model_validate(branch))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.put("/{gym_id}", response_model=Response[GymResponse])
async def update_branch(
    gym_id: UUID,
    data: GymUpdate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Update branch details (name, address, city, phone).
    Only org admin can update branches.
    """
    service = GymService(db)
    try:
        branch = await service.update_branch(
            gym_id,
            current_staff.org_id,
            data,
            current_staff.id
        )
        await db.commit()
        return Response(data=GymResponse.model_validate(branch))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.delete("/{gym_id}", response_model=Response[MessageResponse])
async def delete_branch(
    gym_id: UUID,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Soft delete a branch (set is_active=False).
    Only org admin can delete branches.
    """
    service = GymService(db)
    try:
        await service.delete_branch(gym_id, current_staff.org_id)
        await db.commit()
        return Response(data=MessageResponse(message="Branch deleted successfully"))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.post("/{gym_id}/tax-config", response_model=Response[TaxConfigResponse])
async def create_update_tax_config(
    gym_id: UUID,
    data: TaxConfigCreate,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Create or update GST configuration for a branch.
    Deactivates previous active config and creates a new one.
    """
    service = GymService(db)
    try:
        # Verify branch exists in org
        await service.get_branch(gym_id, current_staff.org_id)
        config = await service.create_or_update_tax_config(gym_id, data, current_staff.id)
        await db.commit()
        return Response(data=TaxConfigResponse.model_validate(config))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.get("/{gym_id}/tax-config", response_model=Response[TaxConfigResponse])
async def get_tax_config(
    gym_id: UUID,
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get active tax configuration for a branch.
    """
    service = GymService(db)
    try:
        # Verify branch exists in org
        await service.get_branch(gym_id, current_staff.org_id)
        config = await service.get_tax_config(gym_id)
        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": "No active tax configuration found", "error_code": "NOT_FOUND"}
            )
        return Response(data=TaxConfigResponse.model_validate(config))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.delete("/{gym_id}/tax-config", response_model=Response[MessageResponse])
async def delete_tax_config(
    gym_id: UUID,
    current_staff: Staff = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Deactivate the active tax configuration for a branch.
    """
    service = GymService(db)
    try:
        # Verify branch exists in org
        await service.get_branch(gym_id, current_staff.org_id)
        await service.delete_tax_config(gym_id)
        await db.commit()
        return Response(data=MessageResponse(message="Tax configuration deactivated"))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )