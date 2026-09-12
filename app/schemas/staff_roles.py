from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator, model_validator
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import json
from app.models.organization_user import BranchStaffRoleEnum

class OrganizationUserCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    is_active: bool = True

class OrganizationUserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None

class OrganizationUserResponse(BaseModel):
    id: UUID
    org_id: UUID
    name: str
    email: EmailStr
    phone: Optional[str] = None
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class BranchStaffRoleCreate(BaseModel):
    user_id: UUID
    role: BranchStaffRoleEnum
    effective_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    effective_to: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("effective_from", "effective_to")
    @classmethod
    def normalize_aware_timestamp(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Role assignment timestamps must include a timezone offset")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_effective_window(self):
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self

class BranchStaffRoleResponse(BaseModel):
    id: UUID
    org_id: UUID
    branch_id: UUID
    user_id: UUID
    role: BranchStaffRoleEnum
    assigned_by: Optional[UUID] = None
    assigned_at: datetime
    effective_from: datetime
    effective_to: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[UUID] = None
    metadata: Optional[Dict[str, Any]] = Field(None, validation_alias="metadata_")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @field_validator("metadata", mode="before")
    @classmethod
    def parse_metadata(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                pass
        return v

class PublicStaffSummary(BaseModel):
    user_id: UUID
    role: BranchStaffRoleEnum
    assigned_at: datetime

    model_config = ConfigDict(from_attributes=True)
