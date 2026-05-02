from pydantic import BaseModel
from typing import List, Optional
from datetime import date


class TodaySummary(BaseModel):
    entries: int
    revenue: float
    expiring_members: int


class AttendancePoint(BaseModel):
    day: date
    entries: int
    denials: int


class RevenuePoint(BaseModel):
    month: date
    total_revenue: float


class DashboardResponse(BaseModel):
    today: TodaySummary
    attendance: List[AttendancePoint]
    revenue: List[RevenuePoint]
