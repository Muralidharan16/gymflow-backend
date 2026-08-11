from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import shared_task
from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import update_session_context, worker_async_session_maker
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.branch_operating_hours import (
    BranchOperatingHours,
    OrganizationOperatingHours,
    BranchSpecialHours,
    BranchHoursProjection,
)


UTC = timezone.utc
_PROJECTION_HORIZON_DAYS = 31
_MAINTENANCE_TOKEN = "branch_hours_projection"


@dataclass(frozen=True)
class ProjectionRebuildResult:
    branch_id: uuid.UUID
    tenant_id: uuid.UUID
    source_hash: str
    current_status: str
    next_refresh_at: Optional[datetime]


@dataclass(frozen=True)
class _OpenInterval:
    starts_at: datetime
    ends_at: datetime
    source: str  # "standard" or "special"


def compute_source_hash(data: dict[str, Any]) -> str:
    """Hash the effective schedule source, not the wall-clock status."""

    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _wall_time_to_utc(
    local_date: date,
    local_time: time,
    tz: ZoneInfo,
    *,
    boundary: str,
) -> datetime:
    """Resolve a local wall time deterministically across DST transitions.

    For an ambiguous fall-back wall time, openings use the earliest occurrence
    and closings use the latest occurrence so an advertised interval is never
    accidentally shortened.  For a nonexistent spring-forward wall time, the
    boundary is normalized forward to the first round-trippable local instant
    produced by ZoneInfo (for example 02:30 -> 03:30 in a one-hour gap).
    """

    naive = datetime.combine(local_date, local_time.replace(tzinfo=None))
    candidates: list[tuple[datetime, datetime]] = []
    round_trips: list[tuple[datetime, datetime]] = []

    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        as_utc = aware.astimezone(UTC)
        round_trip = as_utc.astimezone(tz).replace(tzinfo=None)
        round_trips.append((round_trip, as_utc))
        if round_trip == naive:
            candidates.append((round_trip, as_utc))

    if candidates:
        instants = sorted({candidate[1] for candidate in candidates})
        return instants[0] if boundary == "open" else instants[-1]

    forward = sorted(
        (local_value, instant)
        for local_value, instant in round_trips
        if local_value > naive
    )
    if not forward:
        raise ValueError(
            f"Unable to resolve nonexistent local wall time {naive!s} in {tz.key}"
        )
    return forward[0][1]


def _effective_standard_slots(
    slots: Sequence[BranchOperatingHours | OrganizationOperatingHours],
    schedule_date: date,
) -> list[BranchOperatingHours | OrganizationOperatingHours]:
    return [
        slot
        for slot in slots
        if slot.day_of_week == schedule_date.weekday()
        and slot.valid_from <= schedule_date
        and (slot.valid_until is None or slot.valid_until >= schedule_date)
        and slot.deleted_at is None
    ]


def _intervals_for_slots(
    schedule_date: date,
    slots: Sequence[Any],
    tz: ZoneInfo,
    *,
    source: str,
) -> list[_OpenInterval]:
    intervals: list[_OpenInterval] = []
    for slot in slots:
        if slot.is_closed:
            continue

        if slot.is_24_hours:
            starts_at = _wall_time_to_utc(
                schedule_date,
                time.min,
                tz,
                boundary="open",
            )
            ends_at = _wall_time_to_utc(
                schedule_date + timedelta(days=1),
                time.min,
                tz,
                boundary="close",
            )
        else:
            if slot.open_time is None or slot.close_time is None:
                raise ValueError("Active hours slot is missing open/close time")
            ends_on = (
                schedule_date + timedelta(days=1)
                if slot.close_time <= slot.open_time
                else schedule_date
            )
            starts_at = _wall_time_to_utc(
                schedule_date,
                slot.open_time,
                tz,
                boundary="open",
            )
            ends_at = _wall_time_to_utc(
                ends_on,
                slot.close_time,
                tz,
                boundary="close",
            )

        if ends_at <= starts_at:
            raise ValueError(
                "Resolved operating-hours interval is not positive after timezone conversion"
            )
        intervals.append(
            _OpenInterval(
                starts_at=starts_at,
                ends_at=ends_at,
                source=source,
            )
        )
    return intervals


def _resolve_temporal_state(
    *,
    current_utc: datetime,
    current_local_date: date,
    today_has_special_override: bool,
    today_has_standard_configuration: bool,
    intervals: Sequence[_OpenInterval],
) -> tuple[str, Optional[datetime], Optional[datetime]]:
    ordered = sorted(intervals, key=lambda item: (item.starts_at, item.ends_at))

    current_interval: Optional[_OpenInterval] = None
    if today_has_special_override:
        # A special-date definition owns the local calendar day.  Standard
        # weekly intervals, including a previous-day overnight interval, must
        # not leak through a holiday/exception override.
        current_interval = next(
            (
                item
                for item in ordered
                if item.source == "special"
                and item.starts_at <= current_utc < item.ends_at
            ),
            None,
        )
    else:
        current_interval = next(
            (
                item
                for item in ordered
                if item.starts_at <= current_utc < item.ends_at
            ),
            None,
        )

    if current_interval is not None:
        status = "HOLIDAY" if current_interval.source == "special" else "OPEN"
    elif today_has_special_override or today_has_standard_configuration:
        status = "CLOSED"
    else:
        status = "NOT_CONFIGURED"

    next_open_at = next(
        (item.starts_at for item in ordered if item.starts_at > current_utc),
        None,
    )
    next_close_at = next(
        (item.ends_at for item in ordered if item.ends_at > current_utc),
        None,
    )
    return status, next_open_at, next_close_at


async def rebuild_branch_hours_projection(
    db: AsyncSession,
    branch_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    current_utc: Optional[datetime] = None,
) -> ProjectionRebuildResult:
    """Rebuild one projection inside the caller-owned worker transaction.

    The tenant is an explicit durable input from the claimed outbox row.  The
    branch lookup additionally pins the same tenant, so a malformed/stale job
    cannot cross the RLS boundary even before database policy evaluation.
    """

    now_utc = (current_utc or datetime.now(UTC)).astimezone(UTC)

    branch_stmt = (
        select(OrgBranch.org_id, OrgBranch.timezone)
        .join(
            OrgBranchState,
            (OrgBranchState.branch_id == OrgBranch.id)
            & (OrgBranchState.org_id == OrgBranch.org_id),
        )
        .where(
            OrgBranch.id == branch_id,
            OrgBranch.org_id == tenant_id,
            OrgBranchState.deleted_at.is_(None),
            OrgBranchState.is_active.is_(True),
        )
    )
    branch_info = (await db.execute(branch_stmt)).first()
    if branch_info is None:
        raise LookupError(
            f"Active branch {branch_id} does not exist in tenant {tenant_id}"
        )

    org_id, tz_string = branch_info
    try:
        tz = ZoneInfo(tz_string)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Branch {branch_id} has invalid IANA timezone {tz_string!r}"
        ) from exc

    current_local = now_utc.astimezone(tz)
    today = current_local.date()
    horizon_end = today + timedelta(days=_PROJECTION_HORIZON_DAYS)

    branch_stmt = (
        select(BranchOperatingHours)
        .where(
            BranchOperatingHours.branch_id == branch_id,
            BranchOperatingHours.deleted_at.is_(None),
            BranchOperatingHours.valid_from <= horizon_end,
            or_(
                BranchOperatingHours.valid_until.is_(None),
                BranchOperatingHours.valid_until >= today - timedelta(days=1),
            ),
        )
        .order_by(
            BranchOperatingHours.day_of_week,
            BranchOperatingHours.slot_index,
            BranchOperatingHours.valid_from,
        )
    )
    branch_hours = list((await db.scalars(branch_stmt)).all())

    if branch_hours:
        standard_hours: list[BranchOperatingHours | OrganizationOperatingHours] = (
            branch_hours
        )
    else:
        org_stmt = (
            select(OrganizationOperatingHours)
            .where(
                OrganizationOperatingHours.org_id == org_id,
                OrganizationOperatingHours.deleted_at.is_(None),
                OrganizationOperatingHours.valid_from <= horizon_end,
                or_(
                    OrganizationOperatingHours.valid_until.is_(None),
                    OrganizationOperatingHours.valid_until >= today - timedelta(days=1),
                ),
            )
            .order_by(
                OrganizationOperatingHours.day_of_week,
                OrganizationOperatingHours.slot_index,
                OrganizationOperatingHours.valid_from,
            )
        )
        standard_hours = list((await db.scalars(org_stmt)).all())

    special_stmt = (
        select(BranchSpecialHours)
        .where(
            BranchSpecialHours.branch_id == branch_id,
            BranchSpecialHours.deleted_at.is_(None),
            BranchSpecialHours.special_date >= today - timedelta(days=1),
            BranchSpecialHours.special_date <= horizon_end,
        )
        .order_by(
            BranchSpecialHours.special_date,
            BranchSpecialHours.open_time,
        )
    )
    special_hours = list((await db.scalars(special_stmt)).all())
    special_by_date: dict[date, list[BranchSpecialHours]] = {}
    for slot in special_hours:
        special_by_date.setdefault(slot.special_date, []).append(slot)

    intervals: list[_OpenInterval] = []
    for day_offset in range(-1, _PROJECTION_HORIZON_DAYS + 1):
        schedule_date = today + timedelta(days=day_offset)
        special_slots = special_by_date.get(schedule_date)
        if special_slots:
            intervals.extend(
                _intervals_for_slots(
                    schedule_date,
                    special_slots,
                    tz,
                    source="special",
                )
            )
            continue

        intervals.extend(
            _intervals_for_slots(
                schedule_date,
                _effective_standard_slots(standard_hours, schedule_date),
                tz,
                source="standard",
            )
        )

    today_special = special_by_date.get(today, [])
    today_standard = _effective_standard_slots(standard_hours, today)
    status, next_open_at, next_close_at = _resolve_temporal_state(
        current_utc=now_utc,
        current_local_date=today,
        today_has_special_override=bool(today_special),
        today_has_standard_configuration=bool(today_standard),
        intervals=intervals,
    )

    weekly_schedule: dict[str, list[dict[str, Any]]] = {
        str(day): [] for day in range(7)
    }
    for slot in standard_hours:
        weekly_schedule[str(slot.day_of_week)].append(
            {
                "slot_index": slot.slot_index,
                "valid_from": slot.valid_from.isoformat(),
                "valid_until": slot.valid_until.isoformat()
                if slot.valid_until
                else None,
                "is_closed": slot.is_closed,
                "is_24_hours": slot.is_24_hours,
                "open_time": slot.open_time.isoformat() if slot.open_time else None,
                "close_time": slot.close_time.isoformat() if slot.close_time else None,
            }
        )

    upcoming_exceptions = [
        {
            "date": slot.special_date.isoformat(),
            "reason": slot.reason,
            "is_closed": slot.is_closed,
            "is_24_hours": slot.is_24_hours,
            "open_time": slot.open_time.isoformat() if slot.open_time else None,
            "close_time": slot.close_time.isoformat() if slot.close_time else None,
        }
        for slot in special_hours
        if slot.special_date >= today
    ]

    source_data = {
        "timezone": tz_string,
        "weekly_schedule": weekly_schedule,
        "upcoming_exceptions": upcoming_exceptions,
        "source": "branch" if branch_hours else "organization",
    }
    source_hash = compute_source_hash(source_data)

    insert_stmt = insert(BranchHoursProjection).values(
        branch_id=branch_id,
        projection_version=1,
        last_rebuilt_at=now_utc,
        source_hash=source_hash,
        timezone=tz_string,
        current_status=status,
        next_open_at=next_open_at,
        next_close_at=next_close_at,
        weekly_schedule=weekly_schedule,
        upcoming_exceptions=upcoming_exceptions,
    )
    excluded = insert_stmt.excluded
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["branch_id"],
        set_={
            "projection_version": case(
                (
                    BranchHoursProjection.source_hash != excluded.source_hash,
                    BranchHoursProjection.projection_version + 1,
                ),
                else_=BranchHoursProjection.projection_version,
            ),
            "last_rebuilt_at": excluded.last_rebuilt_at,
            "source_hash": excluded.source_hash,
            "timezone": excluded.timezone,
            "current_status": excluded.current_status,
            "next_open_at": excluded.next_open_at,
            "next_close_at": excluded.next_close_at,
            "weekly_schedule": excluded.weekly_schedule,
            "upcoming_exceptions": excluded.upcoming_exceptions,
        },
    )
    await db.execute(upsert_stmt)

    refresh_candidates = [
        instant
        for instant in (next_open_at, next_close_at)
        if instant is not None and instant > now_utc
    ]
    return ProjectionRebuildResult(
        branch_id=branch_id,
        tenant_id=tenant_id,
        source_hash=source_hash,
        current_status=status,
        next_refresh_at=min(refresh_candidates) if refresh_candidates else None,
    )


@shared_task(name="app.tasks.branch_hours_projection.run_projection")
def run_projection(
    branch_id: str,
    tenant_id: str,
    correlation_id: Optional[str] = None,
):
    """Compatibility task using the bounded worker identity and tenant context.

    Durable outbox processing calls the rebuild function directly so projection
    state and the processed marker commit together.  This wrapper remains only
    for explicit operational rebuilds and therefore requires tenant identity.
    """

    async def _runner() -> None:
        branch_uuid = uuid.UUID(branch_id)
        tenant_uuid = uuid.UUID(tenant_id)
        trace = correlation_id or str(uuid.uuid4())
        async with worker_async_session_maker() as db:
            await update_session_context(
                db,
                org_id=str(tenant_uuid),
                trace_id=trace,
                role="branch_hours_worker",
                internal_maintenance=_MAINTENANCE_TOKEN,
            )
            await rebuild_branch_hours_projection(
                db,
                branch_uuid,
                tenant_uuid,
            )
            await db.commit()

    asyncio.run(_runner())
