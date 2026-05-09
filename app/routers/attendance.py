from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.attendance import AccessCheckResponse
from app.schemas.common import Response
from app.services.attendance_service import AttendanceService
from app.repositories.attendance_repo import AttendanceRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository

router = APIRouter(tags=["Attendance"])

@router.get("/check-access/{uid}", response_model=AccessCheckResponse)
async def check_access(uid: str, request: Request, db: AsyncSession = Depends(get_db)):
    service = AttendanceService(AttendanceRepository(db), MemberRepository(db), SubscriptionRepository(db))
    return await service.check_access(request.state.gym_id, uid)
