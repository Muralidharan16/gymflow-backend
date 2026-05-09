from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import date, datetime


class PaymentCreateRequest(BaseModel):
    member_id: str
    plan_id: str
    amount: float = Field(..., gt=0)
    payment_method: Literal['cash', 'upi', 'card', 'bank_transfer']
    payment_source: Literal['frontend', 'admin_panel', 'auto_renewal', 'offline_cash', 'imported'] = 'admin_panel'
    renewal_type: Literal['new_join', 'renewal', 'upgrade', 'downgrade', 'transfer'] = 'new_join'


class PaymentResponse(BaseModel):
    status: str
    payment_id: Optional[str] = None
    subscription_id: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class PaymentRead(BaseModel):
    id: str
    org_id: str
    member_id: Optional[str] = None
    amount: float
    payment_method: str
    payment_source: str
    status: str
    payment_date: datetime

    class Config:
        from_attributes = True
