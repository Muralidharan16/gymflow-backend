from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..schemas.member import MemberCreate, MemberRead, MemberUpdate
from ..models.models import Member, GymBranch, StaffRole

router = APIRouter(prefix="/members", tags=["members"])

MAX_UID_RETRIES = 3


def _generate_member_uid(branch_code: str, seq: int) -> str:
    """Generate human-readable member UID: MEM-CHN001-0001"""
    return f"MEM-{branch_code}-{str(seq).zfill(4)}"


@router.get('/', response_model=List[MemberRead])
async def list_members(
    page: int = 1,
    size: int = 25,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.trainer, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * size
    stmt = (
        select(Member)
        .where(Member.org_id == context.org_id, Member.deleted_at.is_(None))
        .order_by(Member.created_at.desc())
        .offset(offset)
        .limit(size)
    )
    q = await db.execute(stmt)
    return q.scalars().all()


@router.post('/', response_model=MemberRead, status_code=status.HTTP_201_CREATED)
async def create_member(
    payload: MemberCreate,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    # Validate that branch belongs to this org
    branch_stmt = select(GymBranch).where(
        GymBranch.id == payload.home_branch_id,
        GymBranch.org_id == context.org_id,
        GymBranch.deleted_at.is_(None),
    )
    branch_res = await db.execute(branch_stmt)
    branch = branch_res.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid branch")

    # Generate next member_uid with savepoint-based retry (BUG-2 fix)
    count_stmt = select(func.count()).select_from(Member).where(
        Member.org_id == context.org_id,
        Member.home_branch_id == branch.id,
    )
    count_res = await db.execute(count_stmt)
    next_seq = (count_res.scalar() or 0) + 1

    m = None
    for attempt in range(MAX_UID_RETRIES):
        member_uid = _generate_member_uid(branch.branch_code, next_seq + attempt)
        try:
            async with db.begin_nested():
                m = Member(
                    org_id=context.org_id,
                    home_branch_id=branch.id,
                    member_uid=member_uid,
                    name=payload.name,
                    phone=payload.phone,
                    email=payload.email,
                    photo_url=payload.photo_url,
                    notes=payload.notes,
                    created_by=context.staff_id,
                )
                db.add(m)
            break  # Savepoint committed — UID was unique
        except IntegrityError:
            if attempt == MAX_UID_RETRIES - 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Could not generate unique member UID. Please retry."
                )

    await db.commit()
    await db.refresh(m)
    return m


@router.put('/{member_id}', response_model=MemberRead)
async def update_member(
    member_id: str,
    payload: MemberUpdate,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Member).where(Member.id == member_id, Member.org_id == context.org_id, Member.deleted_at.is_(None))
    q = await db.execute(stmt)
    m = q.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(m, field, value)
    m.updated_by = context.staff_id
    await db.commit()
    await db.refresh(m)
    return m


@router.delete('/{member_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(
    member_id: str,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a member."""
    stmt = select(Member).where(Member.id == member_id, Member.org_id == context.org_id, Member.deleted_at.is_(None))
    q = await db.execute(stmt)
    m = q.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    m.deleted_at = datetime.now(timezone.utc)
    m.is_active = False
    await db.commit()
    return None


@router.post('/{member_id}/enroll-fingerprint', response_model=MemberRead)
async def enroll_fingerprint(
    member_id: str,
    fingerprint: dict,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    fingerprint_id = fingerprint.get('fingerprint_id')
    if not fingerprint_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fingerprint_id required")

    # BUG-1 fix: Check if fingerprint_id is already assigned to another member in this org
    dup_stmt = select(Member.id).where(
        Member.org_id == context.org_id,
        Member.fingerprint_id == fingerprint_id,
        Member.id != member_id,
        Member.deleted_at.is_(None),
    )
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This fingerprint is already enrolled for another member"
        )

    stmt = select(Member).where(Member.id == member_id, Member.org_id == context.org_id, Member.deleted_at.is_(None))
    q = await db.execute(stmt)
    m = q.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    m.fingerprint_id = fingerprint_id
    m.updated_by = context.staff_id
    await db.commit()
    await db.refresh(m)
    return m
