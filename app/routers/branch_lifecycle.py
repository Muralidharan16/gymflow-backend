from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from typing import List

from app.core.database import get_db
from app.core.deps import get_current_active_staff, Staff, BranchAccessGuard
from app.schemas.branch_lifecycle import (
    BranchTransitionRequest,
    BranchStatusStateResponse,
    BranchStatusHistoryResponse
)
from app.models.org_branch import OrgBranchState, ActiveOrgBranch
from app.models.branch_lifecycle import BranchStatusHistory
from app.services.branch_lifecycle_service import BranchLifecycleService

router = APIRouter(prefix="/branches", tags=["Branch Lifecycle Control Plane"])

@router.get(
    "",
    summary="List all branches for the organization"
)
async def list_branches(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    from app.models.org_branch import OrgBranch, OrgBranchState
    from app.models.address import OrganizationAddress
    from app.utils.encryption import decrypt_data

    stmt = (
        select(OrgBranch, OrgBranchState, OrganizationAddress)
        .join(OrgBranchState, OrgBranch.id == OrgBranchState.branch_id)
        .outerjoin(OrganizationAddress, OrgBranch.address_id == OrganizationAddress.id)
        .where(OrgBranch.org_id == current_staff.org_id)
        .where(OrgBranchState.deleted_at.is_(None))
    )
    res = await db.execute(stmt)
    rows = res.all()

    # Branch contacts are the canonical branch-level contact source. Onboarding
    # creates primary phone/email rows here, and subsequent branch-contact APIs
    # maintain them. Do not fall back to tenant-root Organization data or to the
    # requesting staff member's identity: either would couple a branch read to
    # unrelated privileges and could publish the wrong person's contact data.
    from app.schemas.branch_contacts import BranchContactORM, ContactKind
    contact_stmt = select(BranchContactORM).where(
        BranchContactORM.org_id == current_staff.org_id,
        BranchContactORM.deleted_at.is_(None),
        BranchContactORM.is_primary == True
    )
    contact_res = await db.execute(contact_stmt)
    contacts = contact_res.scalars().all()

    # Map branch_id -> contact details
    branch_contacts = {}
    for c in contacts:
        if c.branch_id not in branch_contacts:
            branch_contacts[c.branch_id] = {}
        if c.contact_kind == ContactKind.PHONE:
            branch_contacts[c.branch_id]["phone"] = c.display_format or c.phone_e164
        elif c.contact_kind == ContactKind.EMAIL:
            branch_contacts[c.branch_id]["email"] = c.email_raw

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
        contact_email = contacts_dict.get("email") or f"hello@{branch.internal_slug}.com"
        contact_phone = contacts_dict.get("phone") or "Pending Setup"

        result.append({
            "id": str(branch.id),
            "name": branch.branch_name,
            "internal_code": branch.branch_code,
            "status": state.branch_status.upper() if state.branch_status else "ACTIVE",
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "address_id": str(branch.address_id) if branch.address_id else None,
            "address_line1": addr1,
            "address_city": address.city if address else "Pending",
            "address_state": address.state_province if address else "Pending",
            "address_pincode": address.postal_code if address else "000000"
        })
    return {"data": result}

@router.post(
    "/{branch_id}/transition",
    response_model=BranchStatusStateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate a branch status transition saga"
)
async def transition_branch(
    branch_id: UUID,
    req: BranchTransitionRequest,
    background_tasks: BackgroundTasks,
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Triggers the transition of a branch's operational status.
    Uses Transaction A to atomically flip status, set in-progress lock, and write initial log + outbox deindex.
    Then queues Transaction B (Saga Cascade) as a background task.
    """
    service = BranchLifecycleService(db)

    # 1. Execute Transaction A (Atomic Status Flip & Init)
    correlation_id = await service.initiate_transition(
        branch_id=branch_id,
        org_id=current_staff.org_id,
        to_status=req.to_status,
        actor_id=current_staff.id,
        actor_role=current_staff.role,
        reason=req.reason
    )

    # 2. Queue Transaction B (Saga Cascade)
    # Fetch original status before updating (we can get from history or state query, but service has it)
    # To keep code simple, let's select updated state and pass old state to cascade
    # State has already been committed in Transaction A
    stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch_id)
    res = await db.execute(stmt)
    state = res.scalar_one()

    # The original from_status can be read from the snapshot stored in latest history or inferred.
    # Let's read from the latest history record for this correlation_id.
    stmt_hist = select(BranchStatusHistory).where(
        BranchStatusHistory.correlation_id == correlation_id
    )
    res_hist = await db.execute(stmt_hist)
    hist = res_hist.scalar_one()
    from_status = hist.from_status or "active"

    # Run Transaction B in background
    background_tasks.add_task(
        service.execute_saga_cascade,
        branch_id=branch_id,
        org_id=current_staff.org_id,
        from_status=from_status,
        to_status=req.to_status,
        correlation_id=correlation_id,
        actor_id=current_staff.id
    )

    return state


@router.get(
    "/{branch_id}/state",
    response_model=BranchStatusStateResponse,
    summary="Get current branch status and saga state"
)
async def get_branch_state(
    branch_id: UUID,
    current_staff: Staff = Depends(BranchAccessGuard()),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns the current operational status and saga checkpoints of the branch.
    Guarded by BranchAccessGuard to enforce role-status matrix.
    """
    stmt = select(OrgBranchState).where(
        OrgBranchState.branch_id == branch_id,
        OrgBranchState.org_id == current_staff.org_id,
        OrgBranchState.deleted_at.is_(None)
    )
    res = await db.execute(stmt)
    state = res.scalar_one_or_none()
    if not state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    return state


@router.get(
    "/{branch_id}/history",
    response_model=List[BranchStatusHistoryResponse],
    summary="Get append-only audit trail ledger for a branch"
)
async def get_branch_history(
    branch_id: UUID,
    current_staff: Staff = Depends(BranchAccessGuard()),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns the audit history ledger of status transitions for the branch.
    Only authorized roles per the matrix can view this data.
    """
    stmt = select(BranchStatusHistory).where(
        BranchStatusHistory.branch_id == branch_id
    ).order_by(BranchStatusHistory.changed_at.desc())
    res = await db.execute(stmt)
    history = res.scalars().all()
    return history


@router.post(
    "/watchdog/sweep",
    status_code=status.HTTP_200_OK,
    summary="Manually trigger watchdog recovery sweep"
)
async def trigger_watchdog_sweep(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Admin-only or system-level manual watchdog sweep to resolve hung transitions.
    """
    if current_staff.role not in ("owner", "admin", "superadmin", "compliance"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    service = BranchLifecycleService(db)
    await service.run_watchdog_sweep()
    return {"message": "Watchdog sweep completed successfully."}


@router.post(
    "/reconciliation/sweep",
    status_code=status.HTTP_200_OK,
    summary="Manually trigger reconciliation sync sweep"
)
async def trigger_reconciliation_sweep(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Admin-only manual reconciliation sweep to sync search indexes and state projections.
    """
    if current_staff.role not in ("owner", "admin", "superadmin", "compliance"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    service = BranchLifecycleService(db)
    synced_count = await service.run_reconciliation_sweep()
    return {"message": f"Reconciliation sweep completed. Synced {synced_count} branches."}
