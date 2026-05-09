from pydantic import BaseModel, EmailStr, ConfigDict
import uuid
from datetime import datetime
from typing import Optional
from app.models.enums import StaffRole

class SignupRequest(BaseModel):
    org_name: str
    owner_name: str
    email: EmailStr
    password: str
    pan_number: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class StaffResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: EmailStr
    role: StaffRole
    model_config = ConfigDict(from_attributes=True)
