from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from app.models.enums import MemberStatus

VALID_BLOOD_GROUPS = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}

class MeasurementBase(BaseModel):
    measured_on: date
    weight_kg: Optional[Decimal] = None
    height_cm: Optional[Decimal] = None
    body_fat_pct: Optional[Decimal] = None
    notes: Optional[str] = None

class MeasurementCreate(MeasurementBase):
    pass

class MeasurementResponse(MeasurementBase):
    id: uuid.UUID
    member_id: uuid.UUID
    recorded_by: Optional[uuid.UUID] = None
    model_config = ConfigDict(from_attributes=True)

class MemberBase(BaseModel):
    name: str
    phone: Optional[str] = None
    home_branch_id: Optional[uuid.UUID] = None
    email: Optional[EmailStr] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    blood_group: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    address: Optional[str] = None
    height_cm: Optional[Decimal] = None
    weight_kg: Optional[Decimal] = None
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Member name is required")
        if len(value) < 2:
            raise ValueError("Member name must be at least 2 characters")
        return value

    @field_validator("phone", "emergency_contact_phone")
    @classmethod
    def trim_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("emergency_contact_name")
    @classmethod
    def trim_emergency_contact_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("blood_group")
    @classmethod
    def validate_blood_group(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().upper()
        if not value:
            return None
        if value not in VALID_BLOOD_GROUPS:
            raise ValueError("Invalid blood group")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: Optional[date]) -> Optional[date]:
        if value is None:
            return None
        today = date.today()
        if value > today:
            raise ValueError("Date of birth cannot be in the future")
        age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age < 3:
            raise ValueError("Member must be at least 3 years old")
        if age > 120:
            raise ValueError("Member age cannot exceed 120 years")
        return value

class MemberCreate(MemberBase):
    phone: str

class MemberUpdate(MemberBase):
    name: Optional[str] = None
    status: Optional[MemberStatus] = None

class MemberResponse(MemberBase):
    id: uuid.UUID
    gym_id: Optional[uuid.UUID] = None
    org_id: uuid.UUID
    member_uid: str
    member_number: int
    member_display_code: Optional[str] = None
    home_branch_name: Optional[str] = None
    has_active_subscription: bool = False
    active_subscription_id: Optional[uuid.UUID] = None
    qr_token: Optional[str] = None
    status: MemberStatus
    is_active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
