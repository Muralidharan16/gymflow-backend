# FIXED: [FIX 2] Added email field to ExpiringMemberResponse.
# FIXED: [FIX 3] Refactored CollectionSummaryResponse to pivot by payment method per day.
from pydantic import BaseModel
from decimal import Decimal
from typing import List, Optional
from datetime import date


class DashboardResponse(BaseModel):
    total_revenue_month: Decimal
    active_members: int
    new_members_month: int
    expired_month: int
    churned_members: int = 0
    churn_rate: float


class ExpiringMemberResponse(BaseModel):
    member_id: str
    member_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    plan_name: str
    end_date: date
    days_remaining: Optional[int] = None  # Added for convenience


class HourlyCount(BaseModel):
    hour: int
    count: int


class AttendanceHeatmapResponse(BaseModel):
    hours: List[HourlyCount]
    days_analyzed: int


class CollectionSummaryResponse(BaseModel):
    """One row per date with pivoted payment method totals."""
    date: date
    cash: Decimal = Decimal("0")
    upi: Decimal = Decimal("0")
    card: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    count: int = 0


# Keep old names as aliases for backward compat
CollectionReport = CollectionSummaryResponse
ExpiringMembersResponse = ExpiringMemberResponse