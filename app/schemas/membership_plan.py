from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models.membership_plan import DurationUnit, PlanStatus


_MONEY_MIN = Decimal("0")


def _require_aware_datetime(value: datetime | None) -> datetime | None:
    if value is not None and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ValueError(
            "membership plan validity timestamps must include a timezone"
        )
    return value


class MembershipPlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    price: Decimal = Field(
        ...,
        ge=_MONEY_MIN,
        max_digits=12,
        decimal_places=2,
    )
    duration_value: int = Field(..., gt=0)
    duration_unit: DurationUnit
    max_members: int = Field(1, ge=1)
    branch_id: Optional[UUID] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @field_validator("valid_from", "valid_until")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        return _require_aware_datetime(value)

    @model_validator(mode="after")
    def validate_dates(self):
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self


class MembershipPlanUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(
        None,
        ge=_MONEY_MIN,
        max_digits=12,
        decimal_places=2,
    )
    duration_value: Optional[int] = Field(None, gt=0)
    duration_unit: Optional[DurationUnit] = None
    max_members: Optional[int] = Field(None, ge=1)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @field_validator("valid_from", "valid_until")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        return _require_aware_datetime(value)


class MembershipPlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    branch_id: Optional[UUID]
    plan_code: str
    name: str
    description: Optional[str]
    price: Decimal
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

    @field_serializer("price", when_used="json", return_type=float)
    def serialize_price(self, value: Decimal) -> float:
        # Preserve the established JSON-number API contract while keeping all
        # validation and persistence arithmetic Decimal-safe.
        return float(value)
