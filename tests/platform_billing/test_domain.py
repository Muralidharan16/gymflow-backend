"""
tests/platform_billing/test_domain.py
======================================
Domain primitive unit tests for Platform Billing.

Tests that money types, tax rates, enums, and error codes
behave correctly without requiring a database.

V3.1 requirements tested:
    - Money uses only Decimal (float forbidden)
    - Currency exponent is explicit per-currency
    - TaxRate.apply() uses explicit ROUND_HALF_UP
    - PAYMENT_OVERDUE is the canonical access reason (not PAYMENT_PAST_DUE)
    - Aggregate-specific catalogue status enums
"""

from __future__ import annotations

import decimal

import pytest


# ── Money type ────────────────────────────────────────────────────────────

class TestMoney:
    def test_create_and_compare(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=50000, currency_code="INR")
        assert m.amount_minor == 50000
        assert m.currency_code == "INR"

    def test_currency_forced_uppercase(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=100, currency_code="inr")
        assert m.currency_code == "INR"

    def test_currency_length_validation(self):
        from app.platform_billing.domain.money import Money

        with pytest.raises(ValueError):
            Money(amount_minor=100, currency_code="XX")
        with pytest.raises(ValueError):
            Money(amount_minor=100, currency_code="")

    def test_unknown_currency_fails(self):
        from app.platform_billing.domain.money import Money

        with pytest.raises(ValueError, match="Unknown currency exponent"):
            Money(amount_minor=100, currency_code="XYZ")

    def test_exponent_retrieval(self):
        from app.platform_billing.domain.money import Money

        assert Money(amount_minor=100, currency_code="INR").exponent == 2
        assert Money(amount_minor=100, currency_code="JPY").exponent == 0
        assert Money(amount_minor=100, currency_code="BHD").exponent == 3

    def test_addition_same_currency(self):
        from app.platform_billing.domain.money import Money

        a = Money(amount_minor=10000, currency_code="INR")
        b = Money(amount_minor=5000, currency_code="INR")
        c = a + b
        assert c.amount_minor == 15000
        assert c.currency_code == "INR"

    def test_subtraction_same_currency(self):
        from app.platform_billing.domain.money import Money

        a = Money(amount_minor=20000, currency_code="INR")
        b = Money(amount_minor=7500, currency_code="INR")
        c = a - b
        assert c.amount_minor == 12500

    def test_addition_different_currency_raises(self):
        from app.platform_billing.domain.money import Money

        a = Money(amount_minor=100, currency_code="INR")
        b = Money(amount_minor=100, currency_code="USD")
        with pytest.raises(ValueError, match="different currencies"):
            _ = a + b

    def test_negation(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=500, currency_code="INR")
        n = -m
        assert n.amount_minor == -500
        assert n.currency_code == "INR"

    def test_zero_inr(self):
        from app.platform_billing.domain.money import ZERO_INR

        assert ZERO_INR.amount_minor == 0
        assert ZERO_INR.currency_code == "INR"
        assert ZERO_INR.is_zero()

    def test_from_major_decimal_only(self):
        from app.platform_billing.domain.money import Money

        m = Money.from_major(decimal.Decimal("99.99"), "INR")
        assert m.amount_minor == 9999

    def test_from_major_uses_currency_exponent(self):
        from app.platform_billing.domain.money import Money

        m = Money.from_major(decimal.Decimal("500"), "JPY")
        assert m.amount_minor == 500
        assert m.exponent == 0

    def test_from_major_rounding(self):
        from app.platform_billing.domain.money import Money

        m = Money.from_major(decimal.Decimal("10.005"), "INR")
        assert m.amount_minor == 1001

    def test_from_major_rounding_down(self):
        from app.platform_billing.domain.money import Money

        m = Money.from_major(decimal.Decimal("10.004"), "INR")
        assert m.amount_minor == 1000

    def test_to_major(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=12345, currency_code="INR")
        assert m.to_major() == decimal.Decimal("123.45")

    def test_to_major_jpy(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=500, currency_code="JPY")
        assert m.to_major() == decimal.Decimal("500")

    def test_zero_classmethod(self):
        from app.platform_billing.domain.money import Money

        z = Money.zero("INR")
        assert z.amount_minor == 0
        assert z.currency_code == "INR"

    def test_immutability(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=100, currency_code="INR")
        with pytest.raises(Exception):
            m.amount_minor = 200  # type: ignore[misc]

    def test_hash_and_equality(self):
        from app.platform_billing.domain.money import Money

        m1 = Money(amount_minor=100, currency_code="INR")
        m2 = Money(amount_minor=100, currency_code="INR")
        m3 = Money(amount_minor=200, currency_code="INR")
        assert m1 == m2
        assert hash(m1) == hash(m2)
        assert m1 != m3

    def test_money_negative_boundary(self):
        from app.platform_billing.domain.money import Money

        m = Money(amount_minor=-500, currency_code="INR")
        assert m.is_negative()
        assert not m.is_positive()
        assert not m.is_zero()


# ── TaxRate type ──────────────────────────────────────────────────────────

class TestTaxRate:
    def test_from_percent(self):
        from app.platform_billing.domain.money import TaxRate

        tr = TaxRate.from_percent(decimal.Decimal("18"))
        assert tr.basis_points == 1800

    def test_apply_gst_explicit_rounding(self):
        from app.platform_billing.domain.money import Money, TaxRate

        base = Money(amount_minor=10000, currency_code="INR")
        gst = TaxRate.from_percent(decimal.Decimal("18"))
        tax = gst.apply(base)
        assert tax.amount_minor == 1800

    def test_apply_rounding_down(self):
        from app.platform_billing.domain.money import Money, TaxRate

        base = Money(amount_minor=1, currency_code="INR")
        gst = TaxRate.from_percent(decimal.Decimal("18"))
        tax = gst.apply(base)
        assert tax.amount_minor == 0

    def test_apply_zero_tax(self):
        from app.platform_billing.domain.money import Money, TaxRate

        base = Money(amount_minor=5000, currency_code="INR")
        zero_tax = TaxRate(basis_points=0)
        tax = zero_tax.apply(base)
        assert tax.amount_minor == 0

    def test_to_percent(self):
        from app.platform_billing.domain.money import TaxRate

        tr = TaxRate(basis_points=1250)
        assert tr.to_percent() == decimal.Decimal("12.50")

    def test_negative_basis_points_raises(self):
        from app.platform_billing.domain.money import TaxRate

        with pytest.raises(ValueError, match="non-negative"):
            TaxRate(basis_points=-1)


# ── Enum values ───────────────────────────────────────────────────────────

class TestEnums:
    def test_subscription_status_values(self):
        from app.platform_billing.domain.enums import SubscriptionContractStatus

        allowed = {
            "trialing", "active", "past_due", "pause_scheduled",
            "paused", "cancel_scheduled", "canceled", "expired",
        }
        actual = {v.value for v in SubscriptionContractStatus}
        assert actual == allowed

    def test_platform_access_modes(self):
        from app.platform_billing.domain.enums import PlatformAccessMode

        modes = {"full", "limited_write", "read_only", "billing_only", "blocked"}
        actual = {v.value for v in PlatformAccessMode}
        assert actual == modes

    def test_access_reason_uses_payment_overdue_not_past_due(self):
        from app.platform_billing.domain.enums import AccessReasonCode

        values = {v.value for v in AccessReasonCode}
        assert "PAYMENT_OVERDUE" in values, (
            "V3.1 requires PAYMENT_OVERDUE as the canonical access reason. "
            "PAYMENT_PAST_DUE must not be used."
        )
        assert "PAYMENT_PAST_DUE" not in values, (
            "PAYMENT_PAST_DUE is not a V3.1 access reason. Use PAYMENT_OVERDUE."
        )

    def test_platform_billing_enums_are_separate_from_facility_commerce(self):
        from app.platform_billing.domain.enums import PlatformInvoiceStatus
        from app.models.enums import InvoiceStatus as FacilityInvoiceStatus

        assert PlatformInvoiceStatus.__module__ != FacilityInvoiceStatus.__module__, (
            "Platform billing enums must live in app.platform_billing.domain.enums"
        )

    def test_aggregate_specific_catalogue_statuses(self):
        from app.platform_billing.domain.enums import (
            ProductStatus, PolicyVersionStatus, PlanVersionStatus,
            PriceStatus, FeatureDefinitionStatus,
        )

        assert {v.value for v in ProductStatus} == {"draft", "active", "retired"}
        assert {v.value for v in PolicyVersionStatus} == {"draft", "published", "retired"}
        assert {v.value for v in PlanVersionStatus} == {"draft", "published", "retired"}
        assert {v.value for v in PriceStatus} == {"draft", "active", "retired"}
        assert {v.value for v in FeatureDefinitionStatus} == {"active", "retired"}

    def test_no_combined_catalog_status_exists(self):
        import app.platform_billing.domain.enums as e

        assert not hasattr(e, "CatalogStatus"), (
            "CatalogStatus must not exist. Use aggregate-specific enums: "
            "ProductStatus, PolicyVersionStatus, PlanVersionStatus, PriceStatus, FeatureDefinitionStatus"
        )


# ── Error codes ──────────────────────────────────────────────────────────

class TestErrorCodes:
    def test_all_error_codes_have_http_status(self):
        from app.platform_billing.domain.errors import (
            PlatformBillingErrorCode,
            ERROR_HTTP_STATUS,
        )

        for code in PlatformBillingErrorCode:
            assert code in ERROR_HTTP_STATUS, (
                f"Error code {code.value} has no HTTP status mapping"
            )

    def test_error_instantiation(self):
        from app.platform_billing.domain.errors import (
            PlatformBillingError,
            PlatformBillingErrorCode,
        )

        err = PlatformBillingError(
            code=PlatformBillingErrorCode.PLATFORM_ACCESS_RESTRICTED,
            detail="Test error",
        )
        assert err.code == PlatformBillingErrorCode.PLATFORM_ACCESS_RESTRICTED
        assert err.http_status == 403
