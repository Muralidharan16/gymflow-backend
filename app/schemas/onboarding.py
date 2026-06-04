# app/schemas/onboarding.py
from pydantic import BaseModel, Field, ConfigDict, field_validator, HttpUrl
from datetime import datetime
from typing import Optional

class PincodeLookupResponse(BaseModel):
    city: str
    state: str
    district: str

class OnboardingCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    
    phone: str = Field(..., pattern=r"^(\+91)?[6-9]\d{9}$")
    country_code: str = "IN"
    address_line1: str = Field(..., min_length=3, alias="address_line_1")
    address_line2: Optional[str] = Field(None, alias="address_line_2")
    city: str
    state: str
    pincode: str = Field(..., pattern=r"^[1-9][0-9]{5}$")
    
    # Optional Branding
    tagline: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    year_established: Optional[int] = Field(None, ge=1800)
    
    # Online Presence
    website_url: Optional[str] = None
    social_links: Optional[dict] = Field(default_factory=dict)

    @field_validator("year_established")
    @classmethod
    def validate_year(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            current_year = datetime.now().year
            if v > current_year:
                raise ValueError("Year established cannot be in the future")
        return v

class OnboardingStatusResponse(BaseModel):
    onboarding_completed: bool
    trial_status: str
    days_remaining: int
    soft_lock_at: Optional[datetime]
    hard_lock_at: Optional[datetime]
