from pydantic import BaseModel
from typing import Optional
from datetime import date


class SubscriptionCreate(BaseModel):
    member_id: str
    plan_id: str
    start_date: Optional[date] = None


class SubscriptionRenew(BaseModel):
    renewal_plan_id: Optional[str] = None


class ExpiringMember(BaseModel):
    member_id: str
    member_name: str
    member_uid: str
    phone: Optional[str] = None
    end_date: date

    class Config:
        from_attributes = True
