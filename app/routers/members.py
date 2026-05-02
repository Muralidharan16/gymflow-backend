from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..middleware.auth_middleware import get_current_owner
from ..schemas.member import MemberCreate, MemberRead, MemberUpdate
from ..models.models import Member

router = APIRouter(prefix="/members", tags=["members"])


@router.get('/', response_model=List[MemberRead])
async def list_members(page: int = 1, size: int = 25, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    offset = (page - 1) * size
    q = await db.execute(select(Member).where(Member.gym_id == owner.gym_id).offset(offset).limit(size))
    rows = q.scalars().all()
    return rows


@router.post('/', response_model=MemberRead, status_code=status.HTTP_201_CREATED)
async def create_member(payload: MemberCreate, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    m = Member(gym_id=owner.gym_id, name=payload.name, phone=payload.phone, email=payload.email, photo_url=payload.photo_url)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


@router.put('/{member_id}', response_model=MemberRead)
async def update_member(member_id: str, payload: MemberUpdate, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    q = await db.execute(select(Member).where(Member.id == member_id, Member.gym_id == owner.gym_id))
    m = q.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')
    if payload.name is not None:
        m.name = payload.name
    if payload.phone is not None:
        m.phone = payload.phone
    if payload.email is not None:
        m.email = payload.email
    if payload.photo_url is not None:
        m.photo_url = payload.photo_url
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


@router.delete('/{member_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(member_id: str, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    q = await db.execute(select(Member).where(Member.id == member_id, Member.gym_id == owner.gym_id))
    m = q.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')
    await db.delete(m)
    await db.commit()
    return None


@router.post('/{member_id}/enroll-fingerprint', response_model=MemberRead)
async def enroll_fingerprint(member_id: str, fingerprint: dict, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    fingerprint_id = fingerprint.get('fingerprint_id')
    if not fingerprint_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='fingerprint_id required')
    q = await db.execute(select(Member).where(Member.id == member_id, Member.gym_id == owner.gym_id))
    m = q.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')
    m.fingerprint_id = fingerprint_id
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m
