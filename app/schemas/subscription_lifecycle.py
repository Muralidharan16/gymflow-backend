from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.subscription_lifecycle import (
    SubscriptionEventType,
    SubscriptionFreezeStatus,
    SubscriptionOperationalStatus,
    SubscriptionSeriesStatus,
    SubscriptionSlotRole,
    SubscriptionTermSourceType,
    SubscriptionTermStatus,
)


class MemberBriefResponse(BaseModel):
    id: uuid.UUID
    name: str
    member_number: int | None = None
    phone: str | None = None


class BranchBriefResponse(BaseModel):
    id: uuid.UUID
    name: str
    code: str | None = None
    timezone: str | None = None


class FreezeSummaryResponse(BaseModel):
    id: uuid.UUID
    status: SubscriptionFreezeStatus
    requested_starts_on: date
    planned_ends_on: date | None = None
    actual_ended_on: date | None = None
    extension_days: int
    reason: str | None = None


class TermSummaryResponse(BaseModel):
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
    renewed_from_term_id: uuid.UUID | None = None
    renewal_child_term_id: uuid.UUID | None = None
    capacity: int
    assignment_count: int = 0
    freeze: FreezeSummaryResponse | None = None


class SlotSummaryResponse(BaseModel):
    id: uuid.UUID
    slot_index: int
    role: SubscriptionSlotRole
    current_member: MemberBriefResponse | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    is_vacant: bool


class TimelineItemResponse(BaseModel):
    id: uuid.UUID
    event_type: SubscriptionEventType
    event_at: datetime
    actor_user_id: uuid.UUID | None = None
    term_id: uuid.UUID | None = None
    metadata: dict


class SeriesSummaryResponse(BaseModel):
    id: uuid.UUID
    series_code: str
    org_id: uuid.UUID
    branch: BranchBriefResponse | None = None
    primary_member: MemberBriefResponse
    lifecycle_status: SubscriptionSeriesStatus
    operational_status: SubscriptionOperationalStatus | None = None
    current_term: TermSummaryResponse | None = None
    scheduled_next_term: TermSummaryResponse | None = None
    previous_term_count: int
    capacity: int
    occupied_slots: int
    vacant_slots: int
    current_freeze: FreezeSummaryResponse | None = None
    available_actions: list[str]
    opened_at: datetime | None = None
    archived_at: datetime | None = None


class LifecycleV2ProjectionResponse(BaseModel):
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
