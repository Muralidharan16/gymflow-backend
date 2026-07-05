import re
import uuid
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict

MEMBER_POSTAL_PATTERNS = {
    "IN": r"^\d{6}$",
    "US": r"^\d{5}(-\d{4})?$",
    "GB": r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"
}

class AddressBaseSchema(BaseModel):
    label: Optional[str] = Field(
        None, 
        max_length=100, 
        description="Human-readable branch nickname, e.g., 'Anna Nagar Studio', 'OMR Branch'"
    )
    address_line1: str = Field(..., max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    city: str = Field(..., max_length=100)
    state_province: str = Field(..., max_length=100)
    postal_code: Optional[str] = Field(None, max_length=15)
    country_code: str = Field(..., max_length=2)

class CreateAddressSchema(AddressBaseSchema):
    address_type: str = Field("operational", description="registered, operational, or billing")

    @model_validator(mode="after")
    def validate_billing_requires_address_line1(self) -> "CreateAddressSchema":
        if self.address_type == "billing" and (not self.address_line1 or not self.address_line1.strip()):
            raise ValueError("billing address cannot be vague: address_line1 must be non-empty")
        return self

class PublicAddressSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    city: str
    state_province: str
    country_code: str
    formatted_address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    google_place_id: Optional[str] = None
    maps_url: Optional[str] = None
    maps_embed_allowed: bool = True

    @classmethod
    def serialize_from_db(cls, db_obj) -> "PublicAddressSchema":
        """Helper to serialize from OrganizationAddress DB instance."""
        from app.services.maps_service import serialize_public_maps_data
        maps_data = serialize_public_maps_data(db_obj, db_obj.is_exact_location_visible)
        return cls(
            city=db_obj.city,
            state_province=db_obj.state_province,
            country_code=db_obj.country_code,
            formatted_address=db_obj.formatted_address if db_obj.is_exact_location_visible else None,
            latitude=maps_data["latitude"],
            longitude=maps_data["longitude"],
            google_place_id=maps_data["google_place_id"],
            maps_url=maps_data["maps_url"],
            maps_embed_allowed=maps_data["maps_embed_allowed"]
        )

class PrivateAddressSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: Optional[str] = Field(None, max_length=100)
    address_type: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state_province: str
    postal_code: Optional[str] = None
    country_code: str
    is_verified: bool
    verified_at: Optional[datetime] = None
    verification_source: Optional[str] = None
    is_primary: bool
    is_exact_location_visible: bool
    formatted_address: Optional[str] = None
    
    # Maps Fields
    google_place_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    maps_embed_allowed: Optional[bool] = True
    maps_verification_status: Optional[str] = "pending"
    maps_last_verified_at: Optional[datetime] = None
    maps_verification_error: Optional[str] = None
    maps_verification_source: Optional[str] = None
    maps_updated_at: Optional[datetime] = None
    maps_next_retry_at: Optional[datetime] = None
    maps_retry_count: Optional[int] = 0

    deleted_at: Optional[datetime] = None


class UpdateAddressMapsSchema(BaseModel):
    google_place_id: Optional[str] = Field(None, max_length=300)
    maps_embed_allowed: Optional[bool] = None

    @field_validator("google_place_id")
    @classmethod
    def validate_place_id(cls, v: Optional[str]) -> Optional[str]:
        if not v or v.strip() == "":
            return None
        from app.services.maps_service import PLACE_ID_PATTERN
        if not PLACE_ID_PATTERN.match(v.strip()):
            raise ValueError("Invalid Google Place ID format")
        return v.strip()

class MemberAddressBaseSchema(BaseModel):
    address_type: str = Field("operational", description="registered, operational, or billing")
    address_line1: str = Field(..., max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    city: str = Field(..., max_length=100)
    state_province: str = Field(..., max_length=100)
    postal_code: Optional[str] = Field(None, max_length=15)
    country_code: str = Field(..., max_length=2)

    @field_validator("postal_code")
    @classmethod
    def validate_postal_code(cls, value: Optional[str], info) -> Optional[str]:
        if value is None or value.strip() == "":
            return None
        postal = value.strip().upper()
        country = info.data.get("country_code", "IN").upper()
        pattern = MEMBER_POSTAL_PATTERNS.get(country)
        if pattern and not re.match(pattern, postal):
            raise ValueError(f"Invalid ZIP format '{postal}' for country: {country}")
        return postal

    @model_validator(mode="after")
    def validate_billing_requires_address_line1(self) -> "MemberAddressBaseSchema":
        if self.address_type == "billing" and (not self.address_line1 or not self.address_line1.strip()):
            raise ValueError("billing address cannot be vague: address_line1 must be non-empty")
        return self

class PublicMemberAddressSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    city: str
    state_province: str
    country_code: str

class PrivateMemberAddressSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    member_id: uuid.UUID
    address_type: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state_province: str
    postal_code: Optional[str] = None
    country_code: str
    is_verified: bool
    is_primary: bool
    latitude: Optional[float] = None
    longitude: Optional[float] = None
