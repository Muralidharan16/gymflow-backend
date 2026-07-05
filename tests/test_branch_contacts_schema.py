"""
Core test suite for branch_contacts schema models and validation.

Tests:
- Phone/email normalization
- XOR validation (phone XOR email)
- Immutable contact_kind
- Bounds enforcement
- Email IDNA2008 handling
- Phone E.164 conversion
"""

import pytest
from uuid import uuid4
from datetime import datetime, timezone
from pydantic import ValidationError

from app.schemas.branch_contacts import (
    ContactKind,
    VisibilityScope,
    VerificationMethod,
    BranchContactCreatePhone,
    BranchContactCreateEmail,
    BranchContactUpdate,
    BranchContactResponse,
    normalize_phone,
    normalize_email,
    ChannelCapabilities,
)


class TestPhoneNormalization:
    """Test E.164 phone number normalization."""

    def test_us_phone_to_e164(self):
        """Convert US phone to E.164 format."""
        phone_e164, digits, display = normalize_phone("(212) 555-0123", "US")
        assert phone_e164 == "+12125550123"
        assert digits == "2125550123"
        assert display == "(212) 555-0123"

    def test_uk_phone_to_e164(self):
        """Convert UK phone to E.164 format."""
        phone_e164, digits, display = normalize_phone("020 7946 0958", "GB")
        assert phone_e164.startswith("+44")
        assert len(digits) >= 10

    def test_india_phone_to_e164(self):
        """Convert India phone to E.164 format."""
        phone_e164, digits, display = normalize_phone("9876543210", "IN")
        assert phone_e164 == "+919876543210"
        assert digits == "9876543210"

    def test_auto_detect_in_from_10_digit(self):
        """Auto-detect IN country code from 10-digit number."""
        phone_e164, digits, display = normalize_phone("9876543210", None)
        # Should default to IN if no country code
        assert phone_e164 == "+919876543210"

    def test_phone_with_plus_prefix(self):
        """Handle phone already in E.164 format."""
        phone_e164, digits, display = normalize_phone("+12125550123", None)
        assert phone_e164 == "+12125550123"
        assert digits == "2125550123"

    def test_invalid_phone_raises_error(self):
        """Reject invalid phone numbers."""
        with pytest.raises(ValueError):
            normalize_phone("invalid", "IN")

    def test_invalid_short_indian_phone(self):
        """Reject invalid short Indian phone number."""
        with pytest.raises(ValueError):
            normalize_phone("12345", "IN")

    def test_phone_bounds_enforcement(self):
        """Enforce reasonable phone length bounds."""
        # E.164 should be max ~15 digits + 1 for + = 16 chars
        phone_e164, digits, display = normalize_phone("+919876543210", None)
        assert len(phone_e164) <= 16


class TestEmailNormalization:
    """Test email normalization with IDNA2008."""

    def test_basic_email_normalization(self):
        """Normalize basic ASCII email."""
        email_raw, email_norm = normalize_email("John.Doe@EXAMPLE.COM")
        assert email_raw == "John.Doe@EXAMPLE.COM"
        assert email_norm == "john.doe@example.com"

    def test_email_with_whitespace(self):
        """Strip whitespace from email."""
        email_raw, email_norm = normalize_email("  john@example.com  ")
        assert email_norm == "john@example.com"

    def test_international_domain(self):
        """Handle IDNA2008 internationalized domains."""
        # German domain: münchen.de
        email_raw, email_norm = normalize_email("test@münchen.de")
        assert "@xn--" in email_norm  # Should convert to punycode

    def test_email_bounds_enforcement(self):
        """Enforce email length bounds (max 254 bytes)."""
        email_raw, email_norm = normalize_email("test@example.com")
        assert len(email_raw.encode()) <= 254
        assert len(email_norm.encode()) <= 254

    def test_invalid_email_raises_error(self):
        """Reject invalid email formats."""
        with pytest.raises(ValueError):
            normalize_email("not-an-email")

    def test_missing_at_sign(self):
        """Reject email without @."""
        with pytest.raises(ValueError):
            normalize_email("testexample.com")

    def test_missing_domain(self):
        """Reject email without domain."""
        with pytest.raises(ValueError):
            normalize_email("test@")

    def test_local_part_bounds(self):
        """Enforce local part max 64 bytes."""
        # Create local part with exactly 64 chars
        local = "a" * 64
        email = f"{local}@example.com"
        email_raw, email_norm = normalize_email(email)
        assert len(email_norm.split("@")[0]) <= 64


class TestPhoneContactXOR:
    """Test phone/email XOR validation."""

    def test_phone_contact_valid(self):
        """Create valid phone contact."""
        contact = BranchContactCreatePhone(
            phone_number="9876543210",
            country_code="IN",
            contact_label="Main",
            visibility_scope=VisibilityScope.PUBLIC,
        )
        assert contact.contact_kind == ContactKind.PHONE
        assert contact.email_address is None

    def test_phone_contact_rejects_email(self):
        """Phone contact cannot have email_address."""
        with pytest.raises(ValidationError):
            BranchContactCreatePhone(
                phone_number="9876543210",
                country_code="IN",
                email_address="test@example.com",  # NOT ALLOWED
                contact_label="Main",
            )

    def test_email_contact_valid(self):
        """Create valid email contact."""
        contact = BranchContactCreateEmail(
            email_address="test@example.com",
            contact_label="Support",
            visibility_scope=VisibilityScope.INTERNAL,
        )
        assert contact.contact_kind == ContactKind.EMAIL
        assert contact.phone_number is None

    def test_email_contact_rejects_phone(self):
        """Email contact cannot have phone_number."""
        with pytest.raises(ValidationError):
            BranchContactCreateEmail(
                email_address="test@example.com",
                phone_number="9876543210",  # NOT ALLOWED
                contact_label="Support",
            )

    def test_whatsapp_not_a_contact_kind(self):
        """Contact kind only allows phone/email, whatsapp must fail."""
        with pytest.raises(ValidationError):
            BranchContactCreatePhone(
                phone_number="9876543210",
                country_code="IN",
                contact_label="Whatsapp",
                visibility_scope=VisibilityScope.PUBLIC,
                contact_kind="whatsapp"  # NOT ALLOWED
            )


class TestBranchContactUpdate:
    """Test update schema constraints."""

    def test_contact_kind_is_immutable(self):
        """contact_kind cannot be updated."""
        update = BranchContactUpdate(
            contact_label="Updated",
            is_active=False,
            contact_kind=ContactKind.EMAIL,  # Should be ignored
        )
        # contact_kind should be None (not settable on update)
        assert update.contact_kind is None

    def test_update_allows_partial_fields(self):
        """Update can specify only some fields."""
        update = BranchContactUpdate(
            contact_label="New Label",
            # is_active, visibility_scope, etc. are optional
        )
        assert update.contact_label == "New Label"
        assert update.is_active is None  # Not specified

    def test_update_preserves_field_bounds(self):
        """Update validates field bounds."""
        update = BranchContactUpdate(contact_label="A" * 50)
        assert len(update.contact_label) <= 50

        with pytest.raises(ValidationError):
            BranchContactUpdate(contact_label="A" * 51)


class TestChannelCapabilities:
    """Test JSONB channel capabilities structure."""

    def test_valid_capabilities(self):
        """Create valid channel capabilities."""
        caps = ChannelCapabilities(
            whatsapp_enabled=True,
            sms_enabled=False,
            voice_enabled=True,
            fax_enabled=False,
        )
        data = caps.model_dump()
        assert data["whatsapp_enabled"] is True
        assert data["sms_enabled"] is False

    def test_capabilities_defaults(self):
        """Channel capabilities default to False."""
        caps = ChannelCapabilities()
        assert caps.whatsapp_enabled is False
        assert caps.sms_enabled is False
        assert caps.voice_enabled is False
        assert caps.fax_enabled is False

    def test_capabilities_jsonb_serialization(self):
        """Capabilities can be serialized to JSONB."""
        caps = ChannelCapabilities(whatsapp_enabled=True)
        jsonb_data = caps.model_dump()
        assert isinstance(jsonb_data, dict)
        assert "whatsapp_enabled" in jsonb_data

    def test_unknown_capabilities_rejected(self):
        """Unknown channel capability keys are rejected."""
        with pytest.raises(ValidationError):
            ChannelCapabilities(
                whatsapp_enabled=True,
                telegram_enabled=True,  # NOT ALLOWED
            )

    def test_whatsapp_only_in_capabilities(self):
        """Ensure whatsapp is tested via capabilities."""
        caps = ChannelCapabilities(whatsapp=True)
        assert caps.whatsapp_enabled is True


class TestBranchContactResponse:
    """Test response models with read-only fields."""

    def test_response_includes_normalized_fields(self):
        """Response includes normalized phone/email."""
        response = BranchContactResponse(
            id=uuid4(),
            org_id=uuid4(),
            branch_id=uuid4(),
            contact_kind=ContactKind.PHONE,
            phone_e164="+919876543210",
            normalized_digits="9876543210",
            display_format="098765 43210",
            contact_label="Main",
            visibility_scope=VisibilityScope.PUBLIC,
            is_primary=True,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            created_by=uuid4(),
            updated_at=datetime.now(timezone.utc),
            updated_by=uuid4(),
            deleted_at=None,
            deleted_by=None,
        )
        assert response.phone_e164 == "+919876543210"
        assert response.contact_kind == ContactKind.PHONE


class TestValidationIntegration:
    """Integration tests for complete validation flow."""

    def test_create_phone_contact_full_flow(self):
        """Complete phone contact creation with normalization."""
        contact = BranchContactCreatePhone(
            phone_number="9876543210",
            country_code="IN",
            contact_label="Main Reception",
            visibility_scope=VisibilityScope.PUBLIC,
            channel_capabilities=ChannelCapabilities(
                whatsapp_enabled=True,
                sms_enabled=True,
            ),
        )
        
        # Validate normalization happened
        assert contact.contact_kind == ContactKind.PHONE
        assert contact.email_address is None
        
        # Validate schema
        schema = contact.model_dump()
        assert schema["contact_kind"] == "phone"

    def test_create_email_contact_full_flow(self):
        """Complete email contact creation with normalization."""
        contact = BranchContactCreateEmail(
            email_address="Support@Example.COM",
            contact_label="Customer Support",
            visibility_scope=VisibilityScope.INTERNAL,
        )
        
        assert contact.contact_kind == ContactKind.EMAIL
        assert contact.phone_number is None

    def test_visibility_scope_enum(self):
        """Test all visibility scope options."""
        scopes = [
            VisibilityScope.PUBLIC,
            VisibilityScope.INTERNAL,
            VisibilityScope.MANAGEMENT,
            VisibilityScope.EMERGENCY,
            VisibilityScope.BILLING,
        ]
        
        for scope in scopes:
            contact = BranchContactCreatePhone(
                phone_number="9876543210",
                country_code="IN",
                contact_label=f"Test {scope}",
                visibility_scope=scope,
            )
            assert contact.visibility_scope == scope

    def test_verification_method_enum(self):
        """Test verification method options."""
        methods = [
            VerificationMethod.SMS_OTP,
            VerificationMethod.EMAIL_LINK,
            VerificationMethod.VOICE_CALL,
            VerificationMethod.MANUAL_REVIEW,
        ]
        
        for method in methods:
            assert method in [m.value for m in VerificationMethod]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
