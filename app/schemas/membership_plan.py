from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator

from app.models.membership_plan import PlanStatus, DurationUnit

class MembershipPlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    price: float = Field(..., ge=0)
    duration_value: int = Field(..., gt=0)
    duration_unit: DurationUnit
    max_members: int = Field(1, ge=1)
    branch_id: Optional[UUID] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @field_validator("valid_until")
    def validate_dates(cls, v, info):
        if v and info.data.get("valid_from"):
            if v <= info.data["valid_from"]:
                raise ValueError("valid_until must be after valid_from")
        return v

class MembershipPlanUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[float] = Field(None, ge=0)
    duration_value: Optional[int] = Field(None, gt=0)
    duration_unit: Optional[DurationUnit] = None
    max_members: Optional[int] = Field(None, ge=1)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

class MembershipPlanResponse(BaseModel):
    id: UUID
    org_id: UUID
    branch_id: Optional[UUID]
    plan_code: str
    name: str
    description: Optional[str]
    price: float
    currency: str
    duration_value: int
    duration_unit: DurationUnit
    max_members: int
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    status: PlanStatus
    created_at: datetime
    updated_at: datetime
    archived_at: Optional[datetime]

    class Config:
        from_attributes = True
