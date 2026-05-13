# app/schemas/onboarding.py
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional

class PincodeLookupResponse(BaseModel):
    city: str
    state: str
    district: str

class OnboardingCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    
    phone: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    address_line1: str = Field(..., min_length=5)
    address_line2: Optional[str] = None
    city: str
    state: str
    pincode: str = Field(..., pattern=r"^[1-9][0-9]{5}$")

class OnboardingStatusResponse(BaseModel):
    onboarding_completed: bool
    trial_status: str
    days_remaining: int
    soft_lock_at: Optional[datetime]
    hard_lock_at: Optional[datetime]
