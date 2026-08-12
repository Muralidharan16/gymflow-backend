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
        branch_ids=getattr(request.state, "branch_ids", [])
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

    owns_gym = await db.scalar(
        select(func.public.current_organization_owns_gym(gym_id))
    )
    if not owns_gym:
        raise HTTPException(status_code=404, detail="Gym not found")

    return staff

async def require_trial_active(
    request: Request,
    staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
) -> Staff:
    """
    Enforces trial locking logic:
    - Hard Lock: 403 Forbidden
    - Soft Lock: Block non-GET methods
    """
    trial_service = TrialService(db)
    status_data = await trial_service.get_trial_status(str(staff.org_id))
    
    # 1. Hard Lock Check
    if status_data.get("is_hard_locked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "HARD_LOCKED", "message": "Your account is hard-locked. Please subscribe to continue."}
        )
    
    # 2. Soft Lock Check (Read-Only)
    if status_data.get("is_soft_locked"):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "SOFT_LOCKED", "message": "Your trial has expired. Account is in read-only mode."}
            )
        # Inject header for frontend awareness
        # Note: In FastAPI, adding headers to response from a dependency 
        # is tricky, usually handled in middleware. 
        # But we can at least return the staff and the caller can use it.
    
    return staff


def require_branch_staff_role(
    allowed_roles: list[str],
) -> callable:
    """
    Factory dependency to enforce branch-scoped staff role checks.
    If the caller is an org admin/owner, they bypass the branch role check.
    Otherwise, we check the user's cached branch roles.
    """
    async def dependency(
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db)
    ) -> Staff:
        if staff.role in ("owner", "admin") and staff.gym_id is None:
            return staff
            
        if str(branch_id) not in staff.branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Branch not in authorized scope."
            )

        from app.services.staff_roles_service import StaffRolesService
        service = StaffRolesService(db)
        
        # This checks Redis cache first, falling back to database
        branch_roles = await service.get_user_branch_roles(staff.id, staff.org_id)
        
        user_branch_roles = branch_roles.get(str(branch_id), [])
        for r in allowed_roles:
            if r in user_branch_roles:
                return staff

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You do not have the required role for this branch."
        )
    return dependency


class BranchAccessGuard:
    """
    Evaluates a strict access matrix prior to route execution:
    - active: Owner, Admin, Manager, Trainer.
    - temporarily_closed / under_renovation: Owner, Admin, Manager.
    - compliance_suspended: Owner, Admin. (Managers and Trainers blocked globally).
    - permanently_closed: Owner, Admin (Read-Only ledger queries).
    - unknown status: denied fail-closed.
    """
    def __init__(self, allowed_actions: list[str] = None):
        self.allowed_actions = allowed_actions or []

    async def __call__(
        self,
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db)
    ) -> Staff:
        from app.models.org_branch import OrgBranchState
        from sqlalchemy import select

        stmt = select(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id,
            OrgBranchState.org_id == staff.org_id,
            OrgBranchState.deleted_at.is_(None)
        )
        res = await db.execute(stmt)
        branch_state = res.scalar_one_or_none()
        
        if not branch_state:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found or belongs to another organization"
            )

        status_code = branch_state.status
        role = staff.role

        # Evaluate Matrix
        if status_code == "active":
            if role not in ("owner", "admin", "manager", "trainer", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied for active branch"
                )
        elif status_code in ("temporarily_closed", "under_renovation"):
            if role not in ("owner", "admin", "manager", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is temporarily closed or under renovation."
                )
        elif status_code == "compliance_suspended":
            if role not in ("owner", "admin", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is compliance suspended."
                )
        elif status_code == "permanently_closed":
            if role not in ("owner", "admin", "superadmin", "compliance"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch is permanently closed."
                )
        else:
            # A database/application rollout skew or corrupt lifecycle value must
            # never become an authorization bypass. Unknown states are denied
            # until a deliberate matrix decision is deployed at both layers.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Branch lifecycle status is not recognized."
            )

        # Scope enforcement (for non-owners/non-admins scoped to specific branch)
        if role in ("manager", "trainer") and staff.gym_id is not None:
            if str(branch_id) not in staff.branch_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch not in authorized scope."
                )

        return staff