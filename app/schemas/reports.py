from pydantic import BaseModel
from decimal import Decimal
from typing import List, Dict

class DashboardResponse(BaseModel):
    total_revenue_month: Decimal
    active_members: int
    new_members_month: int
    expired_month: int
    churn_rate: float

class CollectionReport(BaseModel):
    date_from: str
    date_to: str
    total_amount: Decimal
    breakdown: Dict[str, Decimal]

class ExpiringMember(BaseModel):
    member_name: str
    phone: str
    plan_name: str
    end_date: str

class ExpiringMembersResponse(BaseModel):
    members: List[ExpiringMember]
