from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SubscriptionSeriesStatus(str, Enum):
    open = "open"
    closed = "closed"
    archived = "archived"


class SubscriptionTermStatus(str, Enum):
    draft = "draft"
    pending_payment = "pending_payment"
    scheduled = "scheduled"
    active = "active"
    expired = "expired"
    cancelled = "cancelled"
    terminated = "terminated"
    voided = "voided"


class SubscriptionOperationalStatus(str, Enum):
    draft = "draft"
    pending_payment = "pending_payment"
    scheduled = "scheduled"
    active = "active"
    frozen = "frozen"
    expired = "expired"
    cancelled = "cancelled"
    terminated = "terminated"
    voided = "voided"


class SubscriptionTermSourceType(str, Enum):
    admission = "admission"
    renewal = "renewal"
    migration = "migration"
    admin_adjustment = "admin_adjustment"
    plan_change = "plan_change"
    re_enrolment = "re_enrolment"
    administrative_correction = "administrative_correction"


class SubscriptionSlotRole(str, Enum):
    primary = "primary"
    partner = "partner"
    dependent = "dependent"
    family_member = "family_member"
    corporate_member = "corporate_member"
    standard = "standard"


class SubscriptionAssignmentStatus(str, Enum):
    active = "active"
    released = "released"
    voided = "voided"


class SubscriptionFreezeStatus(str, Enum):
    scheduled = "scheduled"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class SubscriptionEventType(str, Enum):
    series_opened = "series_opened"
    admission_created = "admission_created"
    term_scheduled = "term_scheduled"
    term_activated = "term_activated"
    renewal_created = "renewal_created"
    renewal_scheduled = "renewal_scheduled"
    term_expired = "term_expired"
    term_cancelled = "term_cancelled"
    term_terminated = "term_terminated"
    term_voided = "term_voided"
    freeze_scheduled = "freeze_scheduled"
    freeze_started = "freeze_started"
    freeze_resumed = "freeze_resumed"
    freeze_cancelled = "freeze_cancelled"
    series_closed = "series_closed"
    series_archived = "series_archived"
    series_restored = "series_restored"
    slot_assigned = "slot_assigned"
    slot_released = "slot_released"


class SubscriptionIdempotencyState(str, Enum):
    processing = "processing"
    completed = "completed"
    failed = "failed"
    expired = "expired"


class SubscriptionLifecycleError(Exception):
    error_code = "SUBSCRIPTION_LIFECYCLE_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None):
        self.message = message
        if error_code:
            self.error_code = error_code
        super().__init__(message)


class SubscriptionSeriesNotFoundError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_SERIES_NOT_FOUND"


class SubscriptionTermNotFoundError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_TERM_NOT_FOUND"


class SubscriptionTenantMismatchError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_TENANT_MISMATCH"


class MultipleCurrentTermsError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_MULTIPLE_CURRENT_TERMS"

    def __init__(self, series_id: uuid.UUID, term_ids: list[uuid.UUID]):
        self.series_id = series_id
        self.term_ids = term_ids
        super().__init__(
            f"Multiple current subscription terms found for series {series_id}: {term_ids}",
            error_code=self.error_code,
        )


class InvalidSubscriptionLifecycleDataError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_INVALID_LIFECYCLE_DATA"


class UnsupportedSubscriptionStatusError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_UNSUPPORTED_STATUS"


class CorruptRenewalLineageError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_CORRUPT_RENEWAL_LINEAGE"


class SlotAssignmentIntegrityError(SubscriptionLifecycleError):
    error_code = "SUBSCRIPTION_SLOT_ASSIGNMENT_INTEGRITY"


@dataclass(frozen=True)
class Pagination:
    page: int
    size: int
    total: int


@dataclass(frozen=True)
class MemberBrief:
    id: uuid.UUID
    name: str
    member_number: int | None = None
    phone: str | None = None


@dataclass(frozen=True)
class BranchBrief:
    id: uuid.UUID
    name: str
    code: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class FreezeSummary:
    id: uuid.UUID
    status: SubscriptionFreezeStatus
    requested_starts_on: date
    planned_ends_on: date | None
    actual_ended_on: date | None
    extension_days: int
    reason: str | None = None


@dataclass(frozen=True)
class TermSummary:
    id: uuid.UUID
    branch_id: uuid.UUID
    term_code: str
    sequence_number: int
    plan_id: uuid.UUID
    plan_code: str
    plan_name: str
    price: Decimal
    currency_code: str
    duration_value: int
    duration_unit: str
    starts_on: date
    base_ends_on: date
    effective_ends_on: date
    stored_status: SubscriptionTermStatus
    derived_status: SubscriptionOperationalStatus
    source_type: SubscriptionTermSourceType
    renewed_from_term_id: uuid.UUID | None
    renewal_child_term_id: uuid.UUID | None
    capacity: int
    assignment_count: int = 0
    freeze: FreezeSummary | None = None


@dataclass(frozen=True)
class SlotSummary:
    id: uuid.UUID
    slot_index: int
    role: SubscriptionSlotRole
    current_member: MemberBrief | None
    effective_from: date | None
    effective_until: date | None
    is_vacant: bool


@dataclass(frozen=True)
class TimelineItem:
    id: uuid.UUID
    event_type: SubscriptionEventType
    event_at: datetime
    actor_user_id: uuid.UUID | None
    term_id: uuid.UUID | None
    metadata: dict


@dataclass(frozen=True)
class SeriesSummary:
    id: uuid.UUID
    series_code: str
    org_id: uuid.UUID
    branch: BranchBrief | None
    primary_member: MemberBrief
    lifecycle_status: SubscriptionSeriesStatus
    operational_status: SubscriptionOperationalStatus | None
    current_term: TermSummary | None
    scheduled_next_term: TermSummary | None
    previous_term_count: int
    capacity: int
    occupied_slots: int
    vacant_slots: int
    current_freeze: FreezeSummary | None
    available_actions: list[str] = field(default_factory=list)
    opened_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True)
class LifecycleV2Projection:
    id: uuid.UUID
    org_id: uuid.UUID
    branch_id: uuid.UUID
    primary_member_id: uuid.UUID
    membership_plan_id: uuid.UUID
    subscription_code: str
    start_date: date
    end_date: date
    status: SubscriptionOperationalStatus
    price_snapshot: Decimal
    currency_code: str
    duration_value_snapshot: int
    duration_unit_snapshot: str
    max_members_snapshot: int
    assigned_member_ids: list[uuid.UUID]


def business_date_for_timezone(now: datetime, timezone_name: str | None) -> date:
    if timezone_name:
        try:
            return now.astimezone(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError:
            pass
    return now.date()


def resolve_term_status(
    stored_status: SubscriptionTermStatus,
    starts_on: date,
    effective_ends_on: date,
    business_date: date,
    *,
    has_active_freeze: bool = False,
) -> SubscriptionOperationalStatus:
    if stored_status in {
        SubscriptionTermStatus.cancelled,
        SubscriptionTermStatus.terminated,
        SubscriptionTermStatus.voided,
    }:
        return SubscriptionOperationalStatus(stored_status.value)

    if stored_status in {SubscriptionTermStatus.draft, SubscriptionTermStatus.pending_payment}:
        return SubscriptionOperationalStatus(stored_status.value)

    if stored_status == SubscriptionTermStatus.expired:
        return SubscriptionOperationalStatus.expired

    if stored_status not in {SubscriptionTermStatus.active, SubscriptionTermStatus.scheduled}:
        raise UnsupportedSubscriptionStatusError(f"Unsupported subscription term status: {stored_status}")

    if business_date < starts_on:
        return SubscriptionOperationalStatus.scheduled
    if business_date > effective_ends_on:
        return SubscriptionOperationalStatus.expired
    if has_active_freeze:
        return SubscriptionOperationalStatus.frozen
    return SubscriptionOperationalStatus.active


def is_freeze_active(freeze: FreezeSummary | None, business_date: date) -> bool:
    if not freeze or freeze.status != SubscriptionFreezeStatus.active:
        return False
    if business_date < freeze.requested_starts_on:
        return False
    if freeze.planned_ends_on and business_date > freeze.planned_ends_on:
        return False
    if freeze.actual_ended_on and business_date > freeze.actual_ended_on:
        return False
    return True


def available_actions(
    series_status: SubscriptionSeriesStatus,
    derived_term_status: SubscriptionOperationalStatus | None,
    *,
    has_scheduled_renewal: bool = False,
    has_active_freeze: bool = False,
) -> list[str]:
    if series_status == SubscriptionSeriesStatus.archived:
        return ["view", "restore"]

    actions = ["view"]
    if derived_term_status is None:
        actions.extend(["view_history", "archive"])
        return actions

    if derived_term_status == SubscriptionOperationalStatus.active:
        if not has_scheduled_renewal:
            actions.append("schedule_renewal")
        if not has_active_freeze:
            actions.append("freeze")
        actions.extend(["cancel", "terminate"])
    elif derived_term_status == SubscriptionOperationalStatus.frozen:
        actions.extend(["resume", "cancel", "terminate"])
    elif derived_term_status == SubscriptionOperationalStatus.expired:
        actions.extend(["renew", "view_history"])
    elif derived_term_status == SubscriptionOperationalStatus.scheduled:
        actions.append("cancel_scheduled")
    elif derived_term_status in {
        SubscriptionOperationalStatus.cancelled,
        SubscriptionOperationalStatus.terminated,
        SubscriptionOperationalStatus.voided,
    }:
        actions.append("view_history")

    if series_status == SubscriptionSeriesStatus.closed and "archive" not in actions:
        actions.append("archive")
    return actions
