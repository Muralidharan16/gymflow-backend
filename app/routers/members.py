import uuid
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.member import MemberCreate, MemberResponse, MeasurementCreate, MeasurementResponse
from app.schemas.common import Response, PaginatedResponse
from app.services.member_service import MemberService
from app.repositories.member_repo import MemberRepository, MeasurementRepository
from app.repositories.subscription_repo import SubscriptionRepository

router = APIRouter(prefix="/gyms/{gym_id}/members", tags=["Members"])

@router.post("", response_model=Response[MemberResponse])
async def create_member(gym_id: uuid.UUID, data: MemberCreate, request: Request, db: AsyncSession = Depends(get_db)):
    service = MemberService(MemberRepository(db), SubscriptionRepository(db), MeasurementRepository(db), db)
    member = await service.create_member(request.state.org_id, gym_id, data, request.state.staff_id)
    return Response(data=member, message="Member created")

@router.get("", response_model=PaginatedResponse[MemberResponse])
async def list_members(gym_id: uuid.UUID, page: int = 1, size: int = 10, db: AsyncSession = Depends(get_db)):
    repo = MemberRepository(db)
    members = await repo.list(gym_id) # Basic list for now
    total = await repo.count(gym_id)
    return PaginatedResponse(data=members, total=total, page=page, size=size, pages=(total // size) + 1)

@router.post("/{member_id}/measurements", response_model=Response[MeasurementResponse])
async def log_measurement(gym_id: uuid.UUID, member_id: uuid.UUID, data: MeasurementCreate, request: Request, db: AsyncSession = Depends(get_db)):
    service = MemberService(MemberRepository(db), SubscriptionRepository(db), MeasurementRepository(db), db)
    measurement = await service.log_measurement(gym_id, member_id, data, request.state.staff_id)
    return Response(data=measurement, message="Measurement logged")
