from pydantic import BaseModel, ConfigDict, EmailStr
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from app.models.enums import MemberStatus

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
    qr_token: Optional[str] = None
    status: MemberStatus
    is_active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
