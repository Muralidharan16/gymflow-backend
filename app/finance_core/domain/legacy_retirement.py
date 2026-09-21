from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping


class LegacyDisposition(str, Enum):
    historical_read_only = "historical_read_only"
    migrated_finance = "migrated_finance"


class LegacyBatchStatus(str, Enum):
    inventory = "inventory"
    reconciling = "reconciling"
    ready_for_cutover = "ready_for_cutover"
    cutover = "cutover"
    rollback_hold = "rollback_hold"


class LegacyRecordKind(str, Enum):
    invoice = "invoice"
    payment = "payment"
    subscription_link = "subscription_link"


@dataclass(frozen=True)
class LegacyInvoiceSnapshot:
    legacy_invoice_id: str
    organization_id: str
    legacy_invoice_number: str
    legacy_payment_id: str | None
    legacy_subscription_id: str | None
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    legacy_status: str
    source_currency_code: str

    def validate(self) -> None:
        if not self.legacy_invoice_number.strip():
            raise ValueError("PAY-15 legacy invoice number is required")
        for label, value in (
            ("subtotal", self.subtotal),
            ("discount_amount", self.discount_amount),
            ("tax_amount", self.tax_amount),
            ("total_amount", self.total_amount),
        ):
            if value < 0:
                raise ValueError(f"PAY-15 {label} cannot be negative")
        if len(self.source_currency_code) != 3 or self.source_currency_code != self.source_currency_code.upper():
            raise ValueError("PAY-15 legacy invoice currency must be uppercase CHAR(3)")


@dataclass(frozen=True)
class FinanceInvoiceSnapshot:
    finance_invoice_id: str
    organization_id: str
    official_invoice_number: str | None
    subtotal_amount: Decimal
    discount_amount: Decimal
    total_tax_amount: Decimal
    grand_total_amount: Decimal
    currency_code: str
    status: str


@dataclass(frozen=True)
class LegacyPaymentSnapshot:
    legacy_payment_id: str
    organization_id: str
    legacy_subscription_id: str | None
    amount: Decimal
    discount_amount: Decimal
    legacy_status: str
    transaction_reference: str | None
    razorpay_id: str | None
    source_currency_code: str

    def validate(self) -> None:
        if self.amount < 0 or self.discount_amount < 0:
            raise ValueError("PAY-15 legacy payment amounts cannot be negative")
        if len(self.source_currency_code) != 3 or self.source_currency_code != self.source_currency_code.upper():
            raise ValueError("PAY-15 legacy payment currency must be uppercase CHAR(3)")

    @property
    def authoritative_reference(self) -> str | None:
        return self.razorpay_id or self.transaction_reference


@dataclass(frozen=True)
class FinancePaymentSnapshot:
    finance_payment_id: str
    organization_id: str
    amount: Decimal
    currency_code: str
    status: str
    provider_payment_ref: str | None


@dataclass(frozen=True)
class MigrationRecordDecision:
    disposition: str
    exact: bool
    reason_code: str
    source_sha256: str
    target_sha256: str | None


@dataclass(frozen=True)
class RetirementReadiness:
    legacy_write_surfaces_disabled: bool
    source_invoice_count: int
    dispositioned_invoice_count: int
    source_payment_count: int
    dispositioned_payment_count: int
    required_subscription_link_count: int
    preserved_subscription_link_count: int
    unreconciled_migrated_money: int
    unknown_historical_invoices: int
    duplicate_finance_records: int
    source_invoice_total: Decimal
    dispositioned_invoice_total: Decimal
    source_invoice_tax_total: Decimal
    dispositioned_invoice_tax_total: Decimal
    source_payment_total: Decimal
    dispositioned_payment_total: Decimal

    @property
    def ready(self) -> bool:
        return (
            self.legacy_write_surfaces_disabled
            and self.source_invoice_count == self.dispositioned_invoice_count
            and self.source_payment_count == self.dispositioned_payment_count
            and self.required_subscription_link_count == self.preserved_subscription_link_count
            and self.unreconciled_migrated_money == 0
            and self.unknown_historical_invoices == 0
            and self.duplicate_finance_records == 0
            and self.source_invoice_total == self.dispositioned_invoice_total
            and self.source_invoice_tax_total == self.dispositioned_invoice_tax_total
            and self.source_payment_total == self.dispositioned_payment_total
        )

    def require_ready(self) -> None:
        if not self.ready:
            raise ValueError("PAY-15 cutover readiness gate is not satisfied")


def canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pipe_sha256(*values: object | None) -> str:
    canonical = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def invoice_source_sha256(invoice: LegacyInvoiceSnapshot) -> str:
    invoice.validate()
    return _pipe_sha256(
        invoice.legacy_invoice_id,
        invoice.organization_id,
        invoice.legacy_invoice_number,
        invoice.legacy_payment_id,
        invoice.legacy_subscription_id,
        invoice.subtotal,
        invoice.discount_amount,
        invoice.tax_amount,
        invoice.total_amount,
        invoice.legacy_status,
        invoice.source_currency_code,
    )

def payment_source_sha256(payment: LegacyPaymentSnapshot) -> str:
    payment.validate()
    return _pipe_sha256(
        payment.legacy_payment_id,
        payment.organization_id,
        payment.legacy_subscription_id,
        payment.amount,
        payment.discount_amount,
        payment.legacy_status,
        payment.transaction_reference,
        payment.razorpay_id,
        payment.source_currency_code,
    )


def subscription_link_sha256(
    *,
    organization_id: str,
    legacy_subscription_id: str,
    disposition: str,
    modern_subscription_term_id: str | None,
) -> str:
    return _pipe_sha256(
        organization_id,
        legacy_subscription_id,
        disposition,
        modern_subscription_term_id,
    )

def validate_invoice_migration(
    *,
    source: LegacyInvoiceSnapshot,
    disposition: str,
    target: FinanceInvoiceSnapshot | None,
) -> MigrationRecordDecision:
    source.validate()
    if disposition == LegacyDisposition.historical_read_only.value:
        if target is not None:
            raise ValueError("Historical-read-only legacy invoice cannot bind a Finance invoice")
        return MigrationRecordDecision(
            disposition=disposition,
            exact=True,
            reason_code="PAY15_HISTORICAL_READ_ONLY",
            source_sha256=invoice_source_sha256(source),
            target_sha256=None,
        )

    if disposition != LegacyDisposition.migrated_finance.value:
        raise ValueError("Unsupported PAY-15 legacy invoice disposition")
    if target is None:
        raise ValueError("Migrated legacy invoice requires Finance target")
    if target.organization_id != source.organization_id:
        raise ValueError("Legacy/Finance invoice organization mismatch")
    if target.official_invoice_number != source.legacy_invoice_number:
        raise ValueError("Legacy invoice number must be preserved exactly")
    if target.currency_code != source.source_currency_code:
        raise ValueError("Legacy/Finance invoice currency mismatch")
    if (
        target.subtotal_amount != source.subtotal
        or target.discount_amount != source.discount_amount
        or target.total_tax_amount != source.tax_amount
        or target.grand_total_amount != source.total_amount
    ):
        raise ValueError("Legacy/Finance invoice monetary or tax totals mismatch")

    target_hash = _pipe_sha256(
        target.finance_invoice_id,
        target.organization_id,
        target.official_invoice_number,
        target.subtotal_amount,
        target.discount_amount,
        target.total_tax_amount,
        target.grand_total_amount,
        target.currency_code,
        target.status,
    )
    return MigrationRecordDecision(
        disposition=disposition,
        exact=True,
        reason_code="PAY15_FINANCE_INVOICE_EXACT",
        source_sha256=invoice_source_sha256(source),
        target_sha256=target_hash,
    )


def validate_payment_migration(
    *,
    source: LegacyPaymentSnapshot,
    disposition: str,
    target: FinancePaymentSnapshot | None,
) -> MigrationRecordDecision:
    source.validate()
    if disposition == LegacyDisposition.historical_read_only.value:
        if target is not None:
            raise ValueError("Historical-read-only legacy payment cannot bind a Finance payment")
        return MigrationRecordDecision(
            disposition=disposition,
            exact=True,
            reason_code="PAY15_HISTORICAL_READ_ONLY",
            source_sha256=payment_source_sha256(source),
            target_sha256=None,
        )

    if disposition != LegacyDisposition.migrated_finance.value:
        raise ValueError("Unsupported PAY-15 legacy payment disposition")
    if target is None:
        raise ValueError("Migrated legacy payment requires Finance target")
    if target.organization_id != source.organization_id:
        raise ValueError("Legacy/Finance payment organization mismatch")
    if target.amount != source.amount:
        raise ValueError("Legacy/Finance payment amount mismatch")
    if target.currency_code != source.source_currency_code:
        raise ValueError("Legacy/Finance payment currency mismatch")
    if source.authoritative_reference is not None and target.provider_payment_ref != source.authoritative_reference:
        raise ValueError("Legacy payment reference must be preserved exactly")

    target_hash = _pipe_sha256(
        target.finance_payment_id,
        target.organization_id,
        target.amount,
        target.currency_code,
        target.status,
        target.provider_payment_ref,
    )
    return MigrationRecordDecision(
        disposition=disposition,
        exact=True,
        reason_code="PAY15_FINANCE_PAYMENT_EXACT",
        source_sha256=payment_source_sha256(source),
        target_sha256=target_hash,
    )


def batch_manifest_sha256(
    record_hashes: Iterable[str],
    *,
    organization_id: str,
) -> str:
    normalized = sorted(record_hashes)
    for value in normalized:
        if len(value) != 64:
            raise ValueError("PAY-15 manifest requires SHA-256 record hashes")
    return canonical_sha256(
        {
            "phase": "PAY-15",
            "organization_id": organization_id,
            "record_hashes": normalized,
        }
    )
