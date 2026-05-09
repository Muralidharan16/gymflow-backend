from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    duration_days: int = Field(..., gt=0, le=3650)  # Max ~10 years
    price: float = Field(..., gt=0)
    grace_period_days: int = Field(default=3, ge=0, le=30)


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    duration_days: Optional[int] = Field(default=None, gt=0, le=3650)
    price: Optional[float] = Field(default=None, gt=0)
    grace_period_days: Optional[int] = Field(default=None, ge=0, le=30)
    is_active: Optional[bool] = None


class PlanRead(BaseModel):
    id: str
    org_id: str
    name: str
    duration_days: int
    price: float
    grace_period_days: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
