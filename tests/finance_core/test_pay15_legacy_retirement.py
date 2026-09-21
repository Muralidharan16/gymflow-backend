from __future__ import annotations

from decimal import Decimal

import pytest

from app.finance_core.domain.legacy_retirement import (
    FinanceInvoiceSnapshot,
    FinancePaymentSnapshot,
    LegacyInvoiceSnapshot,
    LegacyPaymentSnapshot,
    RetirementReadiness,
    invoice_source_sha256,
    payment_source_sha256,
    subscription_link_sha256,
    validate_invoice_migration,
    validate_payment_migration,
)


def legacy_invoice() -> LegacyInvoiceSnapshot:
    return LegacyInvoiceSnapshot(
        legacy_invoice_id="98000000-0000-0000-0000-000000000601",
        organization_id="98000000-0000-0000-0000-000000000001",
        legacy_invoice_number="LEGACY-INV-0001",
        legacy_payment_id="98000000-0000-0000-0000-000000000501",
        legacy_subscription_id="98000000-0000-0000-0000-000000000401",
        subtotal=Decimal("1000.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("180.00"),
        total_amount=Decimal("1180.00"),
        legacy_status="paid",
        source_currency_code="INR",
    )


def finance_invoice() -> FinanceInvoiceSnapshot:
    return FinanceInvoiceSnapshot(
        finance_invoice_id="99000000-0000-0000-0000-000000000601",
        organization_id="98000000-0000-0000-0000-000000000001",
        official_invoice_number="LEGACY-INV-0001",
        subtotal_amount=Decimal("1000.00"),
        discount_amount=Decimal("0.00"),
        total_tax_amount=Decimal("180.00"),
        grand_total_amount=Decimal("1180.00"),
        currency_code="INR",
        status="paid",
    )


def legacy_payment() -> LegacyPaymentSnapshot:
    return LegacyPaymentSnapshot(
        legacy_payment_id="98000000-0000-0000-0000-000000000501",
        organization_id="98000000-0000-0000-0000-000000000001",
        legacy_subscription_id="98000000-0000-0000-0000-000000000401",
        amount=Decimal("1180.00"),
        discount_amount=Decimal("0.00"),
        legacy_status="completed",
        transaction_reference="legacy-txn-001",
        razorpay_id="pay_legacy_001",
        source_currency_code="INR",
    )


def finance_payment() -> FinancePaymentSnapshot:
    return FinancePaymentSnapshot(
        finance_payment_id="99000000-0000-0000-0000-000000000501",
        organization_id="98000000-0000-0000-0000-000000000001",
        amount=Decimal("1180.00"),
        currency_code="INR",
        status="captured",
        provider_payment_ref="pay_legacy_001",
    )


def test_exact_finance_invoice_migration_preserves_money_tax_and_sequence():
    decision = validate_invoice_migration(
        source=legacy_invoice(),
        disposition="migrated_finance",
        target=finance_invoice(),
    )
    assert decision.exact is True
    assert decision.reason_code == "PAY15_FINANCE_INVOICE_EXACT"
    assert len(decision.source_sha256) == 64
    assert len(decision.target_sha256 or "") == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("official_invoice_number", "NEW-NUMBER", "number"),
        ("subtotal_amount", Decimal("999.00"), "monetary"),
        ("total_tax_amount", Decimal("179.99"), "monetary"),
        ("grand_total_amount", Decimal("1179.99"), "monetary"),
        ("currency_code", "USD", "currency"),
    ],
)
def test_invoice_migration_rejects_any_historical_drift(field, value, message):
    target = finance_invoice()
    changed = FinanceInvoiceSnapshot(
        **{**target.__dict__, field: value}
    )
    with pytest.raises(ValueError, match=message):
        validate_invoice_migration(
            source=legacy_invoice(),
            disposition="migrated_finance",
            target=changed,
        )


def test_exact_finance_payment_migration_preserves_amount_and_reference():
    decision = validate_payment_migration(
        source=legacy_payment(),
        disposition="migrated_finance",
        target=finance_payment(),
    )
    assert decision.exact is True
    assert decision.reason_code == "PAY15_FINANCE_PAYMENT_EXACT"
    assert decision.source_sha256 == payment_source_sha256(legacy_payment())


def test_payment_migration_rejects_reference_amount_currency_or_tenant_drift():
    target = finance_payment()
    for field, value in (
        ("provider_payment_ref", "wrong"),
        ("amount", Decimal("1180.01")),
        ("currency_code", "USD"),
        ("organization_id", "98000000-0000-0000-0000-000000000002"),
    ):
        changed = FinancePaymentSnapshot(**{**target.__dict__, field: value})
        with pytest.raises(ValueError):
            validate_payment_migration(
                source=legacy_payment(),
                disposition="migrated_finance",
                target=changed,
            )


def test_historical_read_only_disposition_is_valid_without_dual_record():
    invoice_decision = validate_invoice_migration(
        source=legacy_invoice(),
        disposition="historical_read_only",
        target=None,
    )
    payment_decision = validate_payment_migration(
        source=legacy_payment(),
        disposition="historical_read_only",
        target=None,
    )
    assert invoice_decision.target_sha256 is None
    assert payment_decision.target_sha256 is None


def test_historical_read_only_cannot_secretly_bind_finance_target():
    with pytest.raises(ValueError, match="cannot bind"):
        validate_invoice_migration(
            source=legacy_invoice(),
            disposition="historical_read_only",
            target=finance_invoice(),
        )
    with pytest.raises(ValueError, match="cannot bind"):
        validate_payment_migration(
            source=legacy_payment(),
            disposition="historical_read_only",
            target=finance_payment(),
        )


def test_subscription_binding_checksum_is_deterministic_and_identity_bound():
    first = subscription_link_sha256(
        organization_id=legacy_invoice().organization_id,
        legacy_subscription_id=legacy_invoice().legacy_subscription_id or "",
        disposition="migrated_finance",
        modern_subscription_term_id=None,
    )
    second = subscription_link_sha256(
        organization_id=legacy_invoice().organization_id,
        legacy_subscription_id=legacy_invoice().legacy_subscription_id or "",
        disposition="migrated_finance",
        modern_subscription_term_id=None,
    )
    changed = subscription_link_sha256(
        organization_id=legacy_invoice().organization_id,
        legacy_subscription_id=legacy_invoice().legacy_subscription_id or "",
        disposition="historical_read_only",
        modern_subscription_term_id=None,
    )
    assert first == second
    assert first != changed
    assert len(first) == 64


def test_source_checksums_change_when_monetary_truth_changes():
    original = legacy_invoice()
    changed = LegacyInvoiceSnapshot(
        **{**original.__dict__, "tax_amount": Decimal("181.00")}
    )
    assert invoice_source_sha256(original) != invoice_source_sha256(changed)


def test_cutover_readiness_requires_every_hard_gate_simultaneously():
    ready = RetirementReadiness(
        legacy_write_surfaces_disabled=True,
        source_invoice_count=1,
        dispositioned_invoice_count=1,
        source_payment_count=1,
        dispositioned_payment_count=1,
        required_subscription_link_count=1,
        preserved_subscription_link_count=1,
        unreconciled_migrated_money=0,
        unknown_historical_invoices=0,
        duplicate_finance_records=0,
        source_invoice_total=Decimal("1180.00"),
        dispositioned_invoice_total=Decimal("1180.00"),
        source_invoice_tax_total=Decimal("180.00"),
        dispositioned_invoice_tax_total=Decimal("180.00"),
        source_payment_total=Decimal("1180.00"),
        dispositioned_payment_total=Decimal("1180.00"),
    )
    assert ready.ready is True
    ready.require_ready()

    broken = RetirementReadiness(
        **{**ready.__dict__, "unknown_historical_invoices": 1}
    )
    assert broken.ready is False
    with pytest.raises(ValueError, match="readiness"):
        broken.require_ready()
