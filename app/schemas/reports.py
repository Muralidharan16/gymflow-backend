from pydantic import BaseModel
from decimal import Decimal
from typing import List, Optional
from datetime import date

class DashboardResponse(BaseModel):
    total_revenue_month: Decimal
    active_members: int
    new_members_month: int
    expired_month: int
    churn_rate: float

class ExpiringMemberResponse(BaseModel):
    member_id: str
    member_name: str
    phone: Optional[str] = None
    plan_name: str
    end_date: date
    days_remaining: Optional[int] = None  # Added for convenience

class HourlyCount(BaseModel):
    hour: int
    count: int

class AttendanceHeatmapResponse(BaseModel):
    data: List[HourlyCount]

class CollectionSummaryResponse(BaseModel):
    method: str
    total_amount: Decimal
    count: int

# Keep old names as aliases for backward compat
CollectionReport = CollectionSummaryResponse
ExpiringMembersResponse = ExpiringMemberResponse