from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


MIN_WINDOW = timedelta(hours=1)
MAX_WINDOW = timedelta(days=31)


@dataclass(frozen=True)
class MoneyReconciliation:
    provider_count: int
    provider_amount_minor: int
    finance_count: int
    finance_amount_minor: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MoneyReconciliation":
        return cls(
            provider_count=int(value["provider_count"]),
            provider_amount_minor=int(value["provider_amount_minor"]),
            finance_count=int(value["finance_count"]),
            finance_amount_minor=int(value["finance_amount_minor"]),
        )


@dataclass(frozen=True)
class AllocationInvoiceIntegrity:
    allocation_count: int
    joined_allocation_count: int
    allocation_amount_minor: int
    joined_invoice_allocation_amount_minor: int
    invoice_state_mismatch_count: int
    orphaned_invoice_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AllocationInvoiceIntegrity":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class LedgerIntegrity:
    posted_debit_minor: int
    posted_credit_minor: int
    unbalanced_entry_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LedgerIntegrity":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SubscriptionIntegrity:
    expected_activation_count: int
    actual_activation_count: int
    activation_mismatch_count: int
    unexplained_entitlement_grants: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubscriptionIntegrity":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class PlatformBillingIntegrity:
    checked_state_count: int
    invalid_state_count: int
    open_reconciliation_discrepancy_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlatformBillingIntegrity":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AccountingClosureIntegrity:
    live_closed_run_count: int
    expected_object_count: int
    observed_object_count: int
    resolved_object_count: int
    mismatch_count: int
    retry_count: int
    manual_review_count: int
    incident_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AccountingClosureIntegrity":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AnomalyCounts:
    unexplained_duplicate_charges: int
    duplicate_refunds: int
    lost_payments: int
    orphaned_invoices: int
    unexplained_entitlement_grants: int
    unresolved_cross_tenant_anomalies: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnomalyCounts":
        return cls(**{key: int(value[key]) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class PostLaunchEvidence:
    schema_version: int
    evidence_class: str
    environment: str
    activation_stage: int
    activation_authorization_id: str
    activation_authorization_sha: str
    window_start: datetime
    window_end: datetime
    read_only_snapshot: bool
    reduced_read_role: bool
    contains_raw_customer_or_provider_identifiers: bool
    payments: MoneyReconciliation
    settlements: MoneyReconciliation
    refunds: MoneyReconciliation
    allocations_invoices: AllocationInvoiceIntegrity
    ledger: LedgerIntegrity
    subscriptions: SubscriptionIntegrity
    platform_billing: PlatformBillingIntegrity
    accounting_closure: AccountingClosureIntegrity
    anomalies: AnomalyCounts
    evidence_manifest_sha256: str
    computed_manifest_sha256: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PostLaunchEvidence":
        return cls(
            schema_version=int(value["schema_version"]),
            evidence_class=str(value["evidence_class"]),
            environment=str(value["environment"]),
            activation_stage=int(value["activation_stage"]),
            activation_authorization_id=str(value["activation_authorization_id"]),
            activation_authorization_sha=str(value["activation_authorization_sha"]),
            window_start=_aware_datetime(value["window_start"]),
            window_end=_aware_datetime(value["window_end"]),
            read_only_snapshot=bool(value["read_only_snapshot"]),
            reduced_read_role=bool(value["reduced_read_role"]),
            contains_raw_customer_or_provider_identifiers=bool(
                value["contains_raw_customer_or_provider_identifiers"]
            ),
            payments=MoneyReconciliation.from_dict(value["payments"]),
            settlements=MoneyReconciliation.from_dict(value["settlements"]),
            refunds=MoneyReconciliation.from_dict(value["refunds"]),
            allocations_invoices=AllocationInvoiceIntegrity.from_dict(
                value["allocations_invoices"]
            ),
            ledger=LedgerIntegrity.from_dict(value["ledger"]),
            subscriptions=SubscriptionIntegrity.from_dict(value["subscriptions"]),
            platform_billing=PlatformBillingIntegrity.from_dict(value["platform_billing"]),
            accounting_closure=AccountingClosureIntegrity.from_dict(
                value["accounting_closure"]
            ),
            anomalies=AnomalyCounts.from_dict(value["anomalies"]),
            evidence_manifest_sha256=str(value["evidence_manifest_sha256"]),
            computed_manifest_sha256=canonical_evidence_manifest_sha256(value),
        )


@dataclass(frozen=True)
class CertificationDecision:
    certified: bool
    failures: tuple[str, ...]


def certify_post_launch(evidence: PostLaunchEvidence) -> CertificationDecision:
    failures: list[str] = []

    if evidence.schema_version != 1:
        failures.append("pay23.schema_version.unsupported")
    if evidence.evidence_class != "production_live":
        failures.append("pay23.evidence_class.not_production_live")
    if evidence.environment != "live":
        failures.append("pay23.environment.not_live")
    if evidence.activation_stage < 1:
        failures.append("pay23.activation_stage.not_live")
    if evidence.activation_stage > 5:
        failures.append("pay23.activation_stage.invalid")
    if not evidence.activation_authorization_id.strip():
        failures.append("pay23.activation_authorization.id_missing")
    if not _git_sha(evidence.activation_authorization_sha):
        failures.append("pay23.activation_authorization.sha_invalid")
    if not evidence.read_only_snapshot:
        failures.append("pay23.snapshot.not_read_only")
    if not evidence.reduced_read_role:
        failures.append("pay23.snapshot.role_not_reduced")
    if evidence.contains_raw_customer_or_provider_identifiers:
        failures.append("pay23.snapshot.raw_identifiers_forbidden")

    window = evidence.window_end - evidence.window_start
    if window < MIN_WINDOW:
        failures.append("pay23.window.too_short")
    if window > MAX_WINDOW:
        failures.append("pay23.window.too_large")

    _money_pair_failures(
        failures,
        "payments",
        evidence.payments,
        require_nonzero=True,
    )
    _money_pair_failures(
        failures,
        "settlements",
        evidence.settlements,
        require_nonzero=True,
    )
    _money_pair_failures(failures, "refunds", evidence.refunds)

    allocations = evidence.allocations_invoices
    _nonnegative_dataclass(failures, "allocations", allocations)
    if allocations.allocation_count != allocations.joined_allocation_count:
        failures.append("pay23.allocations.invoice_join_count_mismatch")
    if (
        allocations.allocation_amount_minor
        != allocations.joined_invoice_allocation_amount_minor
    ):
        failures.append("pay23.allocations.invoice_amount_mismatch")
    if allocations.invoice_state_mismatch_count:
        failures.append("pay23.allocations.invoice_state_mismatch")
    if allocations.orphaned_invoice_count:
        failures.append("pay23.invoices.orphaned")

    ledger = evidence.ledger
    _nonnegative_dataclass(failures, "ledger", ledger)
    if ledger.posted_debit_minor != ledger.posted_credit_minor:
        failures.append("pay23.ledger.aggregate_unbalanced")
    if ledger.unbalanced_entry_count:
        failures.append("pay23.ledger.entry_unbalanced")

    subscriptions = evidence.subscriptions
    _nonnegative_dataclass(failures, "subscriptions", subscriptions)
    if subscriptions.expected_activation_count != subscriptions.actual_activation_count:
        failures.append("pay23.subscriptions.activation_count_mismatch")
    if subscriptions.activation_mismatch_count:
        failures.append("pay23.subscriptions.activation_mismatch")
    if subscriptions.unexplained_entitlement_grants:
        failures.append("pay23.subscriptions.unexplained_entitlement_grants")

    platform = evidence.platform_billing
    _nonnegative_dataclass(failures, "platform_billing", platform)
    if platform.checked_state_count <= 0:
        failures.append("pay23.platform_billing.no_state_checked")
    if platform.invalid_state_count:
        failures.append("pay23.platform_billing.invalid_state")
    if platform.open_reconciliation_discrepancy_count:
        failures.append("pay23.platform_billing.open_reconciliation_discrepancy")

    closure = evidence.accounting_closure
    _nonnegative_dataclass(failures, "accounting_closure", closure)
    if closure.live_closed_run_count <= 0:
        failures.append("pay23.accounting_closure.no_live_closed_run")
    if closure.expected_object_count <= 0:
        failures.append("pay23.accounting_closure.no_objects")
    if closure.expected_object_count != closure.observed_object_count:
        failures.append("pay23.accounting_closure.expected_observed_mismatch")
    minimum_provider_objects = (
        evidence.payments.provider_count
        + evidence.settlements.provider_count
        + evidence.refunds.provider_count
    )
    if closure.expected_object_count < minimum_provider_objects:
        failures.append("pay23.accounting_closure.coverage_too_small")
    if closure.resolved_object_count != closure.expected_object_count:
        failures.append("pay23.accounting_closure.not_fully_resolved")
    for name in ("mismatch_count", "retry_count", "manual_review_count", "incident_count"):
        if getattr(closure, name):
            failures.append(f"pay23.accounting_closure.{name}_nonzero")

    anomalies = evidence.anomalies
    _nonnegative_dataclass(failures, "anomalies", anomalies)
    for name in anomalies.__dataclass_fields__:
        if getattr(anomalies, name) != 0:
            failures.append(f"pay23.anomaly.{name}")

    if anomalies.orphaned_invoices != allocations.orphaned_invoice_count:
        failures.append("pay23.anomaly.orphaned_invoice_counter_disagrees")
    if (
        anomalies.unexplained_entitlement_grants
        != subscriptions.unexplained_entitlement_grants
    ):
        failures.append("pay23.anomaly.entitlement_counter_disagrees")

    if not _sha256(evidence.evidence_manifest_sha256):
        failures.append("pay23.evidence_manifest_sha256.invalid")
    elif (
        evidence.evidence_manifest_sha256.lower()
        != evidence.computed_manifest_sha256
    ):
        failures.append("pay23.evidence_manifest_sha256.mismatch")

    return CertificationDecision(
        certified=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def _money_pair_failures(
    failures: list[str],
    name: str,
    pair: MoneyReconciliation,
    *,
    require_nonzero: bool = False,
) -> None:
    _nonnegative_dataclass(failures, name, pair)
    if require_nonzero and pair.provider_count <= 0:
        failures.append(f"pay23.{name}.no_live_activity")
    if pair.provider_count != pair.finance_count:
        failures.append(f"pay23.{name}.count_mismatch")
    if pair.provider_amount_minor != pair.finance_amount_minor:
        failures.append(f"pay23.{name}.amount_mismatch")


def _nonnegative_dataclass(failures: list[str], prefix: str, value: Any) -> None:
    for field_name in value.__dataclass_fields__:
        item = getattr(value, field_name)
        if isinstance(item, int) and item < 0:
            failures.append(f"pay23.{prefix}.{field_name}.negative")


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("PAY-23 evidence timestamps must be timezone-aware")
    return result


def canonical_evidence_manifest_sha256(value: dict[str, Any]) -> str:
    """Hash the exact JSON evidence envelope, excluding its self-hash field."""
    body = {
        key: item
        for key, item in value.items()
        if key != "evidence_manifest_sha256"
    }
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _git_sha(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        len(normalized) == 40
        and all(character in "0123456789abcdef" for character in normalized)
    )


def _sha256(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        len(normalized) == 64
        and all(character in "0123456789abcdef" for character in normalized)
    )
