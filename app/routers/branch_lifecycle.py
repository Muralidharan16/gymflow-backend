from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from typing import List

from app.core.database import get_db
from app.core.deps import get_current_active_staff, Staff, BranchAccessGuard
from app.schemas.branch_lifecycle import (
    BranchTransitionRequest,
    BranchStatusStateResponse,
    BranchStatusHistoryResponse,
)
from app.models.org_branch import OrgBranchState
from app.models.branch_lifecycle import BranchStatusHistory
from app.services.branch_lifecycle_service import BranchLifecycleService

router = APIRouter(prefix="/branches", tags=["Branch Lifecycle Control Plane"])


def _branch_scope_ids(current_staff: Staff) -> list[UUID] | None:
    """Return normalized branch scope for branch-scoped read roles.

    ``manager`` and ``trainer`` are branch-scoped roles. Their scope must be
    determined by the signed ``branch_ids`` claim, never by the optional gym_id
    claim alone. Invalid branch identifiers fail closed by being ignored; an
    empty resulting scope therefore exposes no branch rows.
    """
    if current_staff.role not in ("manager", "trainer"):
        return None

    branch_ids: list[UUID] = []
    for raw_branch_id in current_staff.branch_ids:
        try:
            branch_ids.append(UUID(str(raw_branch_id)))
        except (TypeError, ValueError, AttributeError):
            continue
    return branch_ids


@router.get("", summary="List all branches for the organization")
async def list_branches(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db),
):
    from app.models.org_branch import OrgBranch, OrgBranchState
    from app.models.address import OrganizationAddress
    from app.utils.encryption import decrypt_data

    scoped_branch_ids = _branch_scope_ids(current_staff)

    stmt = (
        select(OrgBranch, OrgBranchState, OrganizationAddress)
        .join(OrgBranchState, OrgBranch.id == OrgBranchState.branch_id)
        .outerjoin(OrganizationAddress, OrgBranch.address_id == OrganizationAddress.id)
        .where(OrgBranch.org_id == current_staff.org_id)
        .where(OrgBranchState.deleted_at.is_(None))
    )
    if scoped_branch_ids is not None:
        if not scoped_branch_ids:
            return {"data": []}
        stmt = stmt.where(OrgBranch.id.in_(scoped_branch_ids))

    rows = (await db.execute(stmt)).all()

    # Branch contacts are the canonical branch-level contact source. Do not
    # fall back to tenant-root Organization data or request-staff identity.
    from app.schemas.branch_contacts import BranchContactORM, ContactKind

    contact_stmt = select(BranchContactORM).where(
        BranchContactORM.org_id == current_staff.org_id,
        BranchContactORM.deleted_at.is_(None),
        BranchContactORM.is_primary.is_(True),
    )
    if scoped_branch_ids is not None:
        contact_stmt = contact_stmt.where(
            BranchContactORM.branch_id.in_(scoped_branch_ids)
        )
    contacts = (await db.execute(contact_stmt)).scalars().all()

    branch_contacts = {}
    for contact in contacts:
        branch_contacts.setdefault(contact.branch_id, {})
        if contact.contact_kind == ContactKind.PHONE:
            branch_contacts[contact.branch_id]["phone"] = (
                contact.display_format or contact.phone_e164
            )
        elif contact.contact_kind == ContactKind.EMAIL:
            branch_contacts[contact.branch_id]["email"] = contact.email_raw

    result = []
    for branch, state, address in rows:
        addr1 = "Address Pending"
        if address and address.address_line1:
            addr1 = address.address_line1
            if addr1.startswith("enc:"):
                try:
                    addr1 = decrypt_data(addr1[4:])
                except Exception:
                    addr1 = addr1[4:]

        contacts_dict = branch_contacts.get(branch.id, {})
        result.append(
            {
                "id": str(branch.id),
                "name": branch.branch_name,
                "internal_code": branch.branch_code,
                "status": state.branch_status.upper()
                if state.branch_status
                else "ACTIVE",
                "contact_email": contacts_dict.get("email")
                or f"hello@{branch.internal_slug}.com",
                "contact_phone": contacts_dict.get("phone") or "Pending Setup",
                "address_id": str(branch.address_id) if branch.address_id else None,
                "address_line1": addr1,
                "address_city": address.city if address else "Pending",
                "address_state": address.state_province if address else "Pending",
                "address_pincode": address.postal_code if address else "000000",
            }
        )
    return {"data": result}


@router.post(
    "/{branch_id}/transition",
    response_model=BranchStatusStateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate a durable branch status transition saga",
)
async def transition_branch(
    branch_id: UUID,
    req: BranchTransitionRequest,
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db),
):
    """Persist Transaction A and durable Transaction-B intent.

    No FastAPI BackgroundTasks are used. ``initiate_transition`` commits the
    state change, append-only records and ``branch.lifecycle_saga`` outbox row
    before this endpoint returns. A canonical Celery worker later claims that
    row with a fresh worker database session.
    """

    service = BranchLifecycleService(db)
    await service.initiate_transition(
        branch_id=branch_id,
        org_id=current_staff.org_id,
        to_status=req.to_status,
        actor_id=current_staff.id,
        actor_role=current_staff.role,
        reason=req.reason,
    )

    # Return the committed state. Transaction B may still be in progress and
    # is intentionally represented as such to the caller.
    state = (
        await db.execute(
            select(OrgBranchState).where(
                OrgBranchState.branch_id == branch_id,
                OrgBranchState.org_id == current_staff.org_id,
            )
        )
    ).scalar_one()
    return state


@router.get(
    "/{branch_id}/state",
    response_model=BranchStatusStateResponse,
    summary="Get current branch status and saga state",
)
async def get_branch_state(
    branch_id: UUID,
    current_staff: Staff = Depends(BranchAccessGuard()),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(OrgBranchState).where(
        OrgBranchState.branch_id == branch_id,
        OrgBranchState.org_id == current_staff.org_id,
        OrgBranchState.deleted_at.is_(None),
    )
    state = (await db.execute(stmt)).scalar_one_or_none()
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found",
        )
    return state


@router.get(
    "/{branch_id}/history",
    response_model=List[BranchStatusHistoryResponse],
    summary="Get append-only audit trail ledger for a branch",
)
async def get_branch_history(
    branch_id: UUID,
    current_staff: Staff = Depends(BranchAccessGuard()),
    db: AsyncSession = Depends(get_db),
):
    del current_staff
    stmt = (
        select(BranchStatusHistory)
        .where(BranchStatusHistory.branch_id == branch_id)
        .order_by(BranchStatusHistory.changed_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


def _require_maintenance_operator(current_staff: Staff) -> None:
    # Cross-tenant watchdog/reconciliation work is a control-plane operation.
    # Tenant owners/admins must never be able to trigger global maintenance,
    # even though the actual database work executes under a separate bounded
    # maintenance identity.
    if current_staff.role not in ("superadmin", "compliance"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )


@router.post(
    "/watchdog/sweep",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a lifecycle watchdog alert sweep",
)
async def trigger_watchdog_sweep(
    current_staff: Staff = Depends(get_current_active_staff),
):
    """Authorize the operator and enqueue cross-tenant maintenance.

    The HTTP request identity never receives watchdog/reconciliation database
    capability. Celery executes the task with the dedicated maintenance login.
    """
    _require_maintenance_operator(current_staff)

    from app.tasks.branch_lifecycle_sweeps import run_watchdog

    task = run_watchdog.delay()
    return {
        "message": "Watchdog sweep accepted for bounded maintenance execution.",
        "task_id": task.id,
    }


@router.post(
    "/reconciliation/sweep",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a lifecycle reconciliation sweep",
)
async def trigger_reconciliation_sweep(
    current_staff: Staff = Depends(get_current_active_staff),
):
    """Authorize the operator and enqueue reconciliation outside the API pool."""
    _require_maintenance_operator(current_staff)

    from app.tasks.branch_lifecycle_sweeps import run_reconciliation

    task = run_reconciliation.delay()
    return {
        "message": "Reconciliation sweep accepted for bounded maintenance execution.",
        "task_id": task.id,
    }
