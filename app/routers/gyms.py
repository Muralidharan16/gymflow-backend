import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.gym import GymCreate, GymResponse, TaxConfigCreate, TaxConfigResponse
from app.schemas.common import Response
from app.services.gym_service import GymService
from app.repositories.gym_repo import GymRepository, TaxRepository

router = APIRouter(prefix="/gyms", tags=["Gyms"])

@router.get("", response_model=Response[List[GymResponse]])
async def list_gyms(request: Request, db: AsyncSession = Depends(get_db)):
    repo = GymRepository(db)
    gyms = await repo.list(request.state.org_id)
    return Response(data=gyms)

@router.post("", response_model=Response[GymResponse])
async def create_gym(request: Request, data: GymCreate, db: AsyncSession = Depends(get_db)):
    service = GymService(GymRepository(db), TaxRepository(db), db)
    gym = await service.create_branch(request.state.org_id, data)
    return Response(data=gym, message="Gym created")

@router.post("/{gym_id}/tax-config", response_model=Response[TaxConfigResponse])
async def update_tax_config(gym_id: uuid.UUID, data: TaxConfigCreate, db: AsyncSession = Depends(get_db)):
    service = GymService(GymRepository(db), TaxRepository(db), db)
    config = await service.update_tax_config(gym_id, data)
    return Response(data=config, message="Tax config updated")
