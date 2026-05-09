from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


class MemberCreate(BaseModel):
    home_branch_id: str
    name: str = Field(..., min_length=1, max_length=255)
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    photo_url: Optional[str] = None
    notes: Optional[str] = None


class MemberUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    photo_url: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class MemberRead(BaseModel):
    id: str
    org_id: str
    home_branch_id: str
    member_uid: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    fingerprint_id: Optional[str] = None
    photo_url: Optional[str] = None
    status: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
