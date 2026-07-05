import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.member_subscription_v2 import ModernSubscriptionStatus, SubscriptionMemberRole
from app.models.membership_plan import DurationUnit


class SubscriptionCreate(BaseModel):
    branch_id: uuid.UUID
    membership_plan_id: uuid.UUID
    primary_member_id: uuid.UUID
    start_date: date | None = None


class SubscriptionMemberResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    subscription_id: uuid.UUID
    member_id: uuid.UUID
    slot_number: int
    role: SubscriptionMemberRole
    is_active: bool
    joined_at: datetime
    left_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    branch_id: uuid.UUID
    membership_plan_id: uuid.UUID
    primary_member_id: uuid.UUID
    subscription_code: str
    start_date: date
    end_date: date
    status: ModernSubscriptionStatus
    price_snapshot: Decimal
    currency_code: str
    duration_value_snapshot: int
    duration_unit_snapshot: DurationUnit
    max_members_snapshot: int
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None
    archived_at: datetime | None = None
    members: list[SubscriptionMemberResponse] = []

    model_config = ConfigDict(from_attributes=True)


class SubscriptionListResponse(BaseModel):
    data: list[SubscriptionResponse]
    total: int
    page: int
    size: int
    pages: int
