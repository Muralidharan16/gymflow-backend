import uuid
from typing import Optional
from fastapi import Request, HTTPException, Depends, status
from pydantic import BaseModel
from app.core.database import get_db
from app.services.trial_service import TrialService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class Staff(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    gym_id: Optional[uuid.UUID]
    role: str
    branch_ids: list[str] = []


def get_current_active_staff(request: Request) -> Staff:
    if not hasattr(request.state, "staff_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")

    gym_id_val = request.state.gym_id
    if gym_id_val == "None" or not gym_id_val:
        gym_id_val = None
    elif isinstance(gym_id_val, str):
        gym_id_val = uuid.UUID(gym_id_val)

    return Staff(
        id=uuid.UUID(request.state.staff_id) if isinstance(request.state.staff_id, str) else request.state.staff_id,
        org_id=uuid.UUID(request.state.org_id) if isinstance(request.state.org_id, str) else request.state.org_id,
        gym_id=gym_id_val,
        role=request.state.role,
        branch_ids=getattr(request.state, "branch_ids", []),
    )


def require_org_admin(staff: Staff = Depends(get_current_active_staff)) -> Staff:
    if staff.role not in ("owner", "admin") or staff.gym_id is not None:
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return staff


async def require_gym_access(
    gym_id: uuid.UUID,
    staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db),
) -> Staff:
    if staff.gym_id is not None and staff.gym_id != gym_id:
        raise HTTPException(status_code=403, detail="Access denied to this branch")

    owns_gym = await db.scalar(select(func.public.current_organization_owns_gym(gym_id)))
    if not owns_gym:
        raise HTTPException(status_code=404, detail="Gym not found")
    return staff


async def require_trial_active(
    request: Request,
    staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db),
) -> Staff:
    trial_service = TrialService(db)
    status_data = await trial_service.get_trial_status(str(staff.org_id))

    if status_data.get("is_hard_locked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "HARD_LOCKED", "message": "Your account is hard-locked. Please subscribe to continue."},
        )

    if status_data.get("is_soft_locked") and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SOFT_LOCKED", "message": "Your trial has expired. Account is in read-only mode."},
        )
    return staff


def require_branch_staff_role(allowed_roles: list[str]) -> callable:
    """Require a verified branch-scoped role for non-org-admin callers."""

    async def dependency(
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db),
    ) -> Staff:
        if staff.role in ("owner", "admin") and staff.gym_id is None:
            return staff

        if str(branch_id) not in staff.branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Branch not in authorized scope.",
            )

        from app.services.staff_roles_service import StaffRolesService

        service = StaffRolesService(db)
        # Branch scope is verified above. Only now may this transaction read the
        # FORCE-RLS branch_staff_roles relation for the authenticated user.
        await service.authorize_staff_roles_read()
        branch_roles = await service.get_user_branch_roles(staff.id, staff.org_id)

        user_branch_roles = branch_roles.get(str(branch_id), [])
        for role in allowed_roles:
            if role in user_branch_roles:
                return staff

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You do not have the required role for this branch.",
        )

    return dependency


class BranchAccessGuard:
    """Evaluate lifecycle and branch-scope access before route execution."""

    def __init__(self, allowed_actions: list[str] = None):
        self.allowed_actions = allowed_actions or []

    async def __call__(
        self,
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db),
    ) -> Staff:
        from app.models.org_branch import OrgBranchState

        stmt = select(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id,
            OrgBranchState.org_id == staff.org_id,
            OrgBranchState.deleted_at.is_(None),
        )
        res = await db.execute(stmt)
        branch_state = res.scalar_one_or_none()

        if not branch_state:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found or belongs to another organization",
            )

        status_code = branch_state.status
        role = staff.role

        if status_code == "active":
            if role not in ("owner", "admin", "manager", "trainer", "superadmin", "compliance"):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied for active branch")
        elif status_code in ("temporarily_closed", "under_renovation"):
            if role not in ("owner", "admin", "manager", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is temporarily closed or under renovation.",
                )
        elif status_code == "compliance_suspended":
            if role not in ("owner", "admin", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is compliance suspended.",
                )
        elif status_code == "permanently_closed":
            if role not in ("owner", "admin", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is permanently closed.",
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Branch lifecycle status is not recognized.",
            )

        if role in ("manager", "trainer") and staff.gym_id is not None:
            if str(branch_id) not in staff.branch_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch not in authorized scope.",
                )

        return staff
