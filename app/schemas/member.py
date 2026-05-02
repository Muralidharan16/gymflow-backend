from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class MemberCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    photo_url: Optional[str] = None


class MemberUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    photo_url: Optional[str] = None


class MemberRead(BaseModel):
    id: str
    gym_id: str
    name: str
    phone: Optional[str]
    email: Optional[EmailStr]
    fingerprint_id: Optional[str]
    photo_url: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
