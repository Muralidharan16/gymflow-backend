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
    business_type: Optional[str] = None
    tagline: Optional[str] = None
    description: Optional[str] = None
    year_established: Optional[int] = None
    website_url: Optional[str] = None
    social_links: dict = {}
    registrations: List[RegistrationResponse] = []
    business_id: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    logo_status: Optional[str] = None
    logo_thumb_url: Optional[str] = None
    logo_medium_url: Optional[str] = None
    logo_full_url: Optional[str] = None
    cover_status: Optional[str] = None
    cover_mobile_url: Optional[str] = None
    cover_tablet_url: Optional[str] = None
    cover_desktop_url: Optional[str] = None

class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    business_type: Optional[str] = Field(None, max_length=50)
    tagline: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    year_established: Optional[int] = Field(None, ge=1800)
    website_url: Optional[str] = Field(None, max_length=255)
    social_links: Optional[dict] = None
    business_id: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None

    @field_validator("year_established")
    @classmethod
    def validate_year(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            current_year = datetime.now().year
            if v > current_year:
                raise ValueError(f"Year established cannot be in the future (max {current_year})")
        return v

    @model_validator(mode="after")
    def validate_non_nullable_profile_fields(self) -> "OrganizationUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Organization name cannot be null")
        if "social_links" in self.model_fields_set and self.social_links is None:
            raise ValueError("Social links cannot be null")
        return self

class LogoUploadUrlResponse(BaseModel):
    upload_url: str
    fields: dict
    upload_id: str
    expires_in: int

class LogoConfirmRequest(BaseModel):
    upload_id: str

class LogoStatusResponse(BaseModel):
    status: str
    logo_thumb_url: Optional[str] = None
    logo_medium_url: Optional[str] = None
    logo_full_url: Optional[str] = None

class CoverConfirmRequest(BaseModel):
    upload_id: str
    focal_y: float = Field(default=0.5, ge=0.0, le=1.0)

class CoverStatusResponse(BaseModel):
    status: str
    cover_mobile_url: Optional[str] = None
    cover_tablet_url: Optional[str] = None
    cover_desktop_url: Optional[str] = None

class AssetUploadUrlResponse(BaseModel):
    upload_url: str
    fields: dict
    upload_id: str
    expires_in: int
