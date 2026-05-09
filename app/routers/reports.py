from fastapi import APIRouter
from app.schemas.reports import DashboardResponse
from app.schemas.common import Response
from decimal import Decimal

router = APIRouter(prefix="/reports", tags=["Reports"])

@router.get("/dashboard", response_model=Response[DashboardResponse])
async def dashboard():
    # Mock data for dashboard
    data = DashboardResponse(
        total_revenue_month=Decimal("50000"),
        active_members=150,
        new_members_month=12,
        expired_month=5,
        churn_rate=3.3
    )
    return Response(data=data)
