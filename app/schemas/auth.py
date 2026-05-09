from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal
from datetime import datetime

class RegisterRequest(BaseModel):
    org_name: str = Field(..., min_length=2, max_length=255)
    owner_name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    branch_name: str = Field(..., min_length=2, max_length=255)

class RegisterResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    refresh_token: str
    expires_at: datetime
    org_id: str
    branch_id: str
    staff_id: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    refresh_token: Optional[str]
    expires_at: Optional[datetime]

class RefreshRequest(BaseModel):
    refresh_token: str
