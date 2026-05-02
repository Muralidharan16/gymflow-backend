from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class PaymentCreate(BaseModel):
    member_id: Optional[str]
    amount: float
    razorpay_id: Optional[str]


class PaymentRead(BaseModel):
    id: str
    gym_id: str
    member_id: Optional[str]
    amount: float
    payment_date: datetime
    razorpay_id: Optional[str]
    status: str

    class Config:
        from_attributes = True
