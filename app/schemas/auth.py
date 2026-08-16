from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, Field
import uuid
import re
from datetime import datetime
from typing import Optional
from app.models.enums import StaffRole, FacilityType


class SignupRequest(BaseModel):
    org_name: str = Field(..., min_length=2, max_length=100)
    owner_name: str
    email: EmailStr
    password: str
    facility_type: FacilityType

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one number")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError("Password must contain at least one special character")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None


class TokenResponse(BaseModel):
    """Internal server-side token carrier. Never serialize this model to browser JSON."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    onboarding_completed: bool = False


class RefreshRequest(BaseModel):
    """Legacy non-browser refresh shape retained for explicit non-browser integrations."""

    refresh_token: str


class SignupStatusRequest(BaseModel):
    email: EmailStr


class StaffResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: EmailStr
    role: StaffRole
    model_config = ConfigDict(from_attributes=True)
