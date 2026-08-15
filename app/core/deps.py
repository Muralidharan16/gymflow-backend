import uuid
from typing import Optional
from fastapi import Request, HTTPException, Depends, status
from pydantic import BaseModel, Field
from app.core.database import get_db
from app.services.trial_service import TrialService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


BRANCH_SCOPED_ROLES = frozenset({"manager", "trainer", "receptionist", "auditor"})


class Staff(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    gym_id: Optional[uuid.UUID]
    role: str
    branch_ids: list[str] = Field(default_factory=list)


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


def _claimed_branch_scope(staff: Staff) -> set[uuid.UUID]:
    branch_ids: set[uuid.UUID] = set()
    for raw_branch_id in staff.branch_ids:
        try:
            branch_ids.add(uuid.UUID(str(raw_branch_id)))
        except (TypeError, ValueError, AttributeError):
            continue
    return branch_ids


async def resolve_authoritative_branch_scope(
    staff: Staff,
    db: AsyncSession,
) -> set[uuid.UUID] | None:
    """Resolve live branch scope for a branch-scoped principal.

    JWT ``branch_ids`` are a signed upper bound, not authorization authority.
    Security-sensitive reads intersect those claims with current active role
    assignments from PostgreSQL, so role revocation takes effect immediately
    even if the JWT is unexpired or Redis role-cache invalidation fails.
    Organization/control-plane roles return ``None`` because their lifecycle
    authorization is governed by their separate tenant/control-plane matrix.
    """
    if staff.role not in BRANCH_SCOPED_ROLES:
        return None

    claimed_scope = _claimed_branch_scope(staff)
    if not claimed_scope:
        return set()

    from app.services.staff_roles_service import StaffRolesService

    service = StaffRolesService(db)
    await service.authorize_staff_roles_read()
    branch_roles = await service.get_authoritative_user_branch_roles(
        staff.id,
        staff.org_id,
    )

    live_scope: set[uuid.UUID] = set()
    for raw_branch_id, roles in branch_roles.items():
        if staff.role not in roles:
            continue
        try:
            live_scope.add(uuid.UUID(str(raw_branch_id)))
        except (TypeError, ValueError, AttributeError):
            continue

    return claimed_scope & live_scope


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
    """Require a verified live branch-scoped role for non-org-admin callers."""

    async def dependency(
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db),
    ) -> Staff:
        if staff.role in ("owner", "admin") and staff.gym_id is None:
            return staff

        if branch_id not in _claimed_branch_scope(staff):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Branch not in authorized scope.",
            )

        from app.services.staff_roles_service import StaffRolesService

        service = StaffRolesService(db)
        # The signed branch claim is only an upper bound. PostgreSQL remains the
        # authority for whether an assignment is active and unrevoked now.
        await service.authorize_staff_roles_read()
        branch_roles = await service.get_authoritative_user_branch_roles(
            staff.id,
            staff.org_id,
        )

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
    """Evaluate lifecycle and live branch-scope access before route execution."""

    def __init__(self, allowed_actions: list[str] = None):
        self.allowed_actions = allowed_actions or []

    async def __call__(
        self,
        branch_id: uuid.UUID,
        staff: Staff = Depends(get_current_active_staff),
        db: AsyncSession = Depends(get_db),
    ) -> Staff:
        from app.models.org_branch import OrgBranchState

        # Resolve branch-scoped authority before reading lifecycle state. This
        # prevents an unassigned branch role from using state/history endpoints
        # as an intra-tenant branch-existence oracle and makes revocation
        # immediate rather than JWT/cache-TTL bound.
        if staff.role in BRANCH_SCOPED_ROLES:
            live_scope = await resolve_authoritative_branch_scope(staff, db)
            if live_scope is None or branch_id not in live_scope:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. Branch not in authorized scope.",
                )

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

        return staff
