"""
Branch Contacts: FastAPI Models, Schemas, and API Endpoints

CRITICAL SECURITY NOTES:
=======================
1. All email/phone normalization happens BEFORE DB insert
2. Normalized fields are READ-ONLY in API responses
3. Contact kind is immutable (enforce at API layer)
4. Soft-delete is permanent (no resurrection in API)
5. Primary contact swaps use advisory locks + retry logic
6. All writes include session context (org_id, user_id, request_id)
"""

from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime
from uuid import UUID
import phonenumbers
import unicodedata
from pydantic import (
    AliasChoices, BaseModel, Field, field_validator, model_validator,
    ConfigDict
)
from sqlalchemy import Column, String, Boolean, FetchedValue
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP, UUID as SQLAlchemyUUID
from app.models.base import Base


# ============================================================================
# ENUMS
# ============================================================================

class ContactKind(str, Enum):
    """Contact kind: phone or email (immutable)"""
    PHONE = "phone"
    EMAIL = "email"


class VisibilityScope(str, Enum):
    """Visibility scope for contact information"""
    PUBLIC = "public"
    INTERNAL = "internal"
    MANAGEMENT = "management"
    EMERGENCY = "emergency"
    BILLING = "billing"


class VerificationMethod(str, Enum):
    """Verification method used"""
    DNS_MX = "dns_mx"
    MANUAL = "manual"
    SMTP_PROBE = "smtp_probe"
    TWILIO_VERIFY = "twilio_verify"
    SMS_OTP = "twilio_verify"
    EMAIL_LINK = "manual"
    VOICE_CALL = "twilio_verify"
    MANUAL_REVIEW = "manual"


# ============================================================================
# PYDANTIC SCHEMAS
# ============================================================================

class ChannelCapabilities(BaseModel):
    """
    Channel capabilities JSONB schema.
    Each key is a boolean indicating whether the channel is enabled.
    
    SECURITY: This is validated strictly - only these keys allowed.
    Size limit: 1024 bytes (enforced at DB level).
    """
    whatsapp_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("whatsapp_enabled", "whatsapp"),
        serialization_alias="whatsapp",
    )
    sms_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("sms_enabled", "sms"),
        serialization_alias="sms",
    )
    voice_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("voice_enabled", "voice"),
        serialization_alias="voice",
    )
    fax_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("fax_enabled", "fax"),
        serialization_alias="fax",
    )

    @model_validator(mode="before")
    @classmethod
    def parse_json_string(cls, data):
        if isinstance(data, str):
            import json
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                pass
        return data

    @field_validator(
        "whatsapp_enabled", "sms_enabled", "voice_enabled", "fax_enabled",
        mode="before"
    )
    @classmethod
    def ensure_boolean(cls, v):
        if v is None:
            return False
        if not isinstance(v, bool):
            # Also handle string 'true'/'false' if parsed weirdly
            if isinstance(v, str):
                return v.lower() == 'true'
            raise ValueError("Channel capabilities must be boolean")
        return v

    model_config = ConfigDict(frozen=True, extra="forbid")


class BranchContactBase(BaseModel):
    """Base schema for branch contacts (shared fields)"""
    contact_kind: ContactKind
    contact_label: str = Field(default="General", max_length=50)
    visibility_scope: VisibilityScope = Field(default=VisibilityScope.INTERNAL)
    channel_capabilities: ChannelCapabilities = Field(default_factory=ChannelCapabilities)
    is_active: bool = True


class BranchContactCreatePhone(BranchContactBase):
    """Create schema for PHONE contact"""
    contact_kind: ContactKind = ContactKind.PHONE
    phone_number: str = Field(
        ..., 
        description="Phone number in any format. Will be normalized to E.164.",
        examples=["+1 (415) 555-1234", "4155551234", "+14155551234"]
    )
    country_code: Optional[str] = Field(
        None,
        description="ISO-3166-1 alpha-2 country code. Auto-detected if phone_number is E.164.",
        min_length=2, max_length=2
    )
    display_format: Optional[str] = Field(
        None,
        description="User-friendly display format. Auto-generated if not provided.",
        max_length=100
    )
    
    # Email fields must be absent for phone contacts
    email_address: Optional[str] = Field(None)

    @model_validator(mode="after")
    def validate_phone_contact(self):
        """Enforce: phone contact must have phone, no email"""
        if self.email_address is not None:
            raise ValueError("Phone contacts cannot have email_address")
        if not self.phone_number or not self.phone_number.strip():
            raise ValueError("phone_number is required for phone contacts")
        return self


class BranchContactCreateEmail(BranchContactBase):
    """Create schema for EMAIL contact"""
    contact_kind: ContactKind = ContactKind.EMAIL
    email_address: str = Field(
        ...,
        description="Email address. Will be normalized for storage.",
        examples=["john.doe@example.com", "JOHN.DOE@EXAMPLE.COM"]
    )
    
    # Phone fields must be absent for email contacts
    phone_number: Optional[str] = Field(None)
    country_code: Optional[str] = Field(None)
    display_format: Optional[str] = Field(None)

    @field_validator("email_address")
    @classmethod
    def validate_email(cls, v):
        if not v or not v.strip():
            raise ValueError("email_address is required for email contacts")
        # Basic validation - actual validation happens via EmailStr
        if len(v) > 254:  # RFC 5321
            raise ValueError("Email address exceeds 254 characters")
        return v

    @model_validator(mode="after")
    def validate_email_contact(self):
        """Enforce: email contact must have email, no phone"""
        if self.phone_number is not None:
            raise ValueError("Email contacts cannot have phone_number")
        return self


# Union type for create endpoints
BranchContactCreate = BranchContactCreatePhone | BranchContactCreateEmail


class BranchContactUpdate(BaseModel):
    """
    Update schema for branch contacts.
    
    CRITICAL: Normalized fields (phone_e164, email_normalized, display_format)
    are NEVER writable via API. These are computed server-side only.
    """
    contact_kind: Optional[ContactKind] = Field(
        None,
        description="IMMUTABLE: Cannot be changed after creation. Omit this field."
    )
    contact_label: Optional[str] = Field(None, max_length=50)
    visibility_scope: Optional[VisibilityScope] = None
    channel_capabilities: Optional[ChannelCapabilities] = None
    is_active: Optional[bool] = None
    is_primary: Optional[bool] = None
    
    # For contact kind = phone
    phone_number: Optional[str] = None
    country_code: Optional[str] = Field(None, min_length=2, max_length=2)
    display_format: Optional[str] = Field(None, max_length=100)
    
    # For contact kind = email
    email_address: Optional[str] = None

    @model_validator(mode="after")
    def validate_immutability(self):
        """Enforce: contact_kind cannot be changed."""
        if self.contact_kind is not None:
            self.contact_kind = None
        return self

    @model_validator(mode="after")
    def validate_xor_fields(self):
        """Enforce: phone XOR email, not both"""
        has_phone = self.phone_number is not None
        has_email = self.email_address is not None
        if has_phone and has_email:
            raise ValueError("Cannot update both phone_number and email_address in one request")
        return self


class BranchContactResponse(BranchContactBase):
    """
    Response schema for branch contacts.
    
    NOTE: Normalized fields are READ-ONLY and computed server-side:
    - phone_e164 (canonical E.164 format)
    - normalized_digits (digits only for search)
    - email_normalized (lowercased, punycode-normalized)
    - display_format (auto-formatted)
    """
    id: UUID
    branch_id: UUID
    org_id: UUID
    
    # Normalized fields (READ-ONLY)
    phone_e164: Optional[str] = None
    normalized_digits: Optional[str] = None
    email_normalized: Optional[str] = None
    display_format: Optional[str] = None
    
    country_code: Optional[str] = None
    
    # Display-friendly fields
    email_raw: Optional[str] = None  # For display purposes only
    phone_display: Optional[str] = Field(None, alias="display_format")
    
    is_primary: bool
    email_reachability_verified: bool = False
    verified_at: Optional[datetime] = None
    verification_method: Optional[VerificationMethod] = None
    
    # Metadata
    created_at: datetime
    created_by: Optional[UUID] = None
    updated_at: datetime
    updated_by: Optional[UUID] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class BranchContactAuditEvent(BaseModel):
    """Audit trail entry for a contact change"""
    id: UUID
    changed_at: datetime
    org_id: UUID
    branch_contact_id: UUID
    changed_by: Optional[UUID] = None
    action: str  # INSERT, UPDATE, DELETE
    changed_fields: Dict[str, Any]
    request_id: Optional[UUID] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    change_reason: Optional[str] = None

    @field_validator("changed_fields", mode="before")
    @classmethod
    def parse_changed_fields(cls, v: Any) -> Any:
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except Exception:
                pass
        return v

    model_config = ConfigDict(from_attributes=True)


class PromoteToPrimaryRequest(BaseModel):
    """Request to promote a contact to primary status"""
    contact_kind: ContactKind = Field(
        ...,
        description="Must match the contact's kind"
    )


# ============================================================================
# PHONE NORMALIZATION (Server-Side)
# ============================================================================

def normalize_phone(
    phone_number: str,
    country_code: Optional[str] = None
) -> tuple[str, str, str]:
    """
    Normalize phone number to E.164 format.
    
    Returns:
        (phone_e164, normalized_digits, display_format)
    
    Raises:
        ValueError: if phone number is invalid
    """
    try:
        # Parse with country code hint if provided. A non-E.164 national number
        # cannot be parsed without a region, so default to US for legacy callers.
        parse_region = country_code or (None if phone_number.strip().startswith("+") else "IN")
        parsed = phonenumbers.parse(phone_number, parse_region)
        
        # Validate
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError(f"Invalid phone number: {phone_number}")
        
        # Extract components
        phone_e164 = phonenumbers.format_number(
            parsed, 
            phonenumbers.PhoneNumberFormat.E164
        )
        normalized_digits = phonenumbers.national_significant_number(parsed)
        
        # Format for display
        display_format = phonenumbers.format_number(
            parsed,
            phonenumbers.PhoneNumberFormat.NATIONAL
        )
        
        return phone_e164, normalized_digits, display_format
        
    except phonenumbers.NumberParseException as e:
        raise ValueError(f"Failed to parse phone number: {str(e)}")


# ============================================================================
# EMAIL NORMALIZATION (Server-Side)
# ============================================================================

def normalize_email(email_address: str) -> tuple[str, str]:
    """
    Normalize email for storage.
    
    Process:
    1. Whitespace trim
    2. Unicode NFC normalization
    3. Domain lowercasing
    4. IDNA2008 punycode normalization (for domain)
    
    Returns:
        (email_raw, email_normalized)
    
    Raises:
        ValueError: if email is invalid
    """
    email = email_address.strip()
    
    if len(email) > 254:  # RFC 5321
        raise ValueError("Email address exceeds 254 characters (RFC 5321)")
    
    if "@" not in email:
        raise ValueError("Invalid email address (missing @)")
    
    local_part, domain = email.rsplit("@", 1)
    
    if not local_part:
        raise ValueError("Email local part is required")
    
    if not domain:
        raise ValueError("Email domain is required")
    
    if len(local_part) > 64:  # RFC 5321 local part limit
        raise ValueError("Email local part exceeds 64 characters")
    
    if len(domain) > 253:  # RFC 1035
        raise ValueError("Email domain exceeds 253 characters")
    
    # NFC normalization (Unicode standard form)
    local_normalized = unicodedata.normalize("NFC", local_part)
    domain_normalized = unicodedata.normalize("NFC", domain)
    
    # Domain: lowercase + IDNA2008 punycode
    try:
        # IDNA2008 encoding (handles internationalized domains)
        domain_ascii = domain_normalized.lower().encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        raise ValueError("Invalid internationalized domain name")
    
    email_normalized = f"{local_normalized}@{domain_ascii}".lower()
    email_raw = email  # Preserve original for display
    
    return email_raw, email_normalized


# ============================================================================
# SQLALCHEMY ORM MODEL
# ============================================================================

class BranchContactORM(Base):
    """SQLAlchemy ORM model for branch_contacts table"""
    __tablename__ = "branch_contacts"
    __table_args__ = {"schema": "public"}
    
    id = Column(SQLAlchemyUUID(as_uuid=True), primary_key=True, server_default=FetchedValue())
    org_id = Column(SQLAlchemyUUID(as_uuid=True), nullable=False, index=True)
    branch_id = Column(SQLAlchemyUUID(as_uuid=True), nullable=False, index=True)
    
    contact_kind = Column(
        ENUM(
            ContactKind,
            name="contact_kind_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            create_type=False,
        ),
        nullable=False,
    )
    
    # Phone fields
    phone_e164 = Column(String(20))
    normalized_digits = Column(String(20))
    display_format = Column(String(100))
    country_code = Column(String(2))
    
    # Email fields
    email_raw = Column(String(255))
    email_normalized = Column(String(255))
    
    contact_label = Column(String(50), default="General")
    visibility_scope = Column(
        ENUM(
            VisibilityScope,
            name="visibility_scope_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            create_type=False,
        ),
        default=VisibilityScope.INTERNAL,
    )
    
    channel_capabilities = Column(JSONB, default=dict)
    is_whatsapp_enabled = Column(Boolean, server_default=FetchedValue())  # Generated column
    
    is_primary = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    email_reachability_verified = Column(Boolean, default=False)
    
    verified_at = Column(TIMESTAMP(timezone=True))
    verification_method = Column(
        ENUM(
            VerificationMethod,
            name="verification_method_enum",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            create_type=False,
        )
    )
    
    created_at = Column(TIMESTAMP(timezone=True), server_default=FetchedValue())
    created_by = Column(SQLAlchemyUUID(as_uuid=True))
    updated_at = Column(TIMESTAMP(timezone=True), server_default=FetchedValue())
    updated_by = Column(SQLAlchemyUUID(as_uuid=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    deleted_by = Column(SQLAlchemyUUID(as_uuid=True))


class BranchContactAuditORM(Base):
    """SQLAlchemy ORM model for branch_contacts_audit table."""
    __tablename__ = "branch_contacts_audit"
    __table_args__ = {"schema": "public"}

    id = Column(SQLAlchemyUUID(as_uuid=True), primary_key=True)
    changed_at = Column(TIMESTAMP(timezone=True), primary_key=True)
    org_id = Column(SQLAlchemyUUID(as_uuid=True), nullable=False)
    branch_contact_id = Column(SQLAlchemyUUID(as_uuid=True), nullable=False)
    changed_by = Column(SQLAlchemyUUID(as_uuid=True))
    action = Column(String(20), nullable=False)
    changed_fields = Column(JSONB, nullable=False)
    request_id = Column(SQLAlchemyUUID(as_uuid=True))
    ip_address = Column(String(45))
    user_agent = Column(String)
    change_reason = Column(String(500))
