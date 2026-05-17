from pydantic import BaseModel, Field, field_validator, model_validator, HttpUrl
import uuid
import re
from typing import Optional, List
from datetime import datetime

class RegistrationCreate(BaseModel):
    id_type: str = Field(..., description="PAN, VAT, EIN, GST, etc.")
    id_number: str = Field(..., min_length=5, max_length=50)
    country_code: str = Field(..., min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_id_format(self) -> "RegistrationCreate":
        if self.id_type.upper() == "PAN" and self.country_code.upper() == "IN":
            pan_regex = r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$"
            if not re.match(pan_regex, self.id_number.upper()):
                raise ValueError("Invalid PAN format. Expected: ABCDE1234F")
            # Normalize to uppercase
            self.id_number = self.id_number.upper()
        return self

class RegistrationResponse(BaseModel):
    id: uuid.UUID
    id_type: str
    id_number_masked: str
    country_code: str
    is_verified: bool
    verified_at: Optional[datetime] = None

class OrganizationProfileResponse(BaseModel):
    id: uuid.UUID
    name: str
    tagline: Optional[str] = None
    description: Optional[str] = None
    year_established: Optional[int] = None
    website_url: Optional[str] = None
    social_links: dict = {}
    registrations: List[RegistrationResponse] = []

class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    tagline: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    year_established: Optional[int] = Field(None, ge=1800)
    website_url: Optional[str] = None
    social_links: Optional[dict] = None

    @field_validator("year_established")
    @classmethod
    def validate_year(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            current_year = datetime.now().year
            if v > current_year:
                raise ValueError("Year established cannot be in the future")
        return v
