from pydantic import BaseModel, ConfigDict
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from app.models.enums import SubscriptionStatus

class PlanBase(BaseModel):
    name: str
    description: Optional[str] = None
    duration_days: int
    price: Decimal
    max_freeze_days: int = 0
    features: dict = {}

class PlanCreate(PlanBase):
    pass

class PlanUpdate(PlanBase):
    name: Optional[str] = None
    duration_days: Optional[int] = None
    price: Optional[Decimal] = None

class PlanResponse(PlanBase):
    id: uuid.UUID
    gym_id: uuid.UUID
    is_active: bool
    model_config = ConfigDict(from_attributes=True)

class SubscriptionBase(BaseModel):
    plan_id: uuid.UUID
    start_date: date

from app.models.enums import SubscriptionStatus, PaymentMethod

class SubscriptionCreate(SubscriptionBase):
    amount_paid: Decimal
    payment_method: PaymentMethod

class SubscriptionResponse(BaseModel):
    id: uuid.UUID
    member_id: uuid.UUID
    plan_id: uuid.UUID
    start_date: date
    end_date: date
    status: SubscriptionStatus
    total_freeze_days: int
    model_config = ConfigDict(from_attributes=True)

class FreezeRequest(BaseModel):
    days: int
    reason: str

class CancelRequest(BaseModel):
    reason: str
