from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.tasks.branch_hours_projection import (
    _intervals_for_slots,
    _mask_standard_intervals_for_special_day,
    _resolve_temporal_state,
    _wall_time_to_utc,
    compute_source_hash,
)


class MockBranchHours:
    def __init__(
        self,
        *,
        is_closed: bool = False,
        is_24_hours: bool = False,
        open_time: time | None = None,
        close_time: time | None = None,
    ) -> None:
        self.is_closed = is_closed
        self.is_24_hours = is_24_hours
        self.open_time = open_time
        self.close_time = close_time


def test_phase2_dst_boundary_math_uses_projection_wall_time_resolver() -> None:
    """The production resolver must honor the post-spring-forward UTC offset."""
    tz = ZoneInfo("America/New_York")

    # 2026 Spring Forward in the US is March 8; March 9 06:00 is EDT (UTC-4).
    resolved = _wall_time_to_utc(
        date(2026, 3, 9),
        time(6, 0),
        tz,
        boundary="open",
    )

    assert resolved == datetime(2026, 3, 9, 10, 0, tzinfo=timezone.utc)


def test_phase2_nonexistent_spring_forward_time_normalizes_forward() -> None:
    """A nonexistent local opening must not be interpreted with a stale offset."""
    tz = ZoneInfo("America/New_York")

    # 02:30 does not exist on 2026-03-08. The hardened resolver normalizes the
    # wall time forward using a round-trippable instant rather than inventing a
    # local timestamp that never occurred.
    resolved = _wall_time_to_utc(
        date(2026, 3, 8),
        time(2, 30),
        tz,
        boundary="open",
    )

    assert resolved.astimezone(tz).replace(tzinfo=None) > datetime(2026, 3, 8, 2, 30)


def test_phase2_status_resolution_uses_special_day_masking() -> None:
    """Special-day intervals own the local date and override standard hours."""
    tz = ZoneInfo("America/New_York")
    current_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)  # 07:00 EST
    schedule_date = current_utc.astimezone(tz).date()

    standard = _intervals_for_slots(
        schedule_date,
        [MockBranchHours(open_time=time(5, 0), close_time=time(23, 0))],
        tz,
        source="standard",
    )
    special = _intervals_for_slots(
        schedule_date,
        [MockBranchHours(open_time=time(6, 0), close_time=time(18, 0))],
        tz,
        source="special",
    )

    intervals = _mask_standard_intervals_for_special_day(
        [*standard, *special],
        special_date=schedule_date,
        tz=tz,
    )
    status, _, _ = _resolve_temporal_state(
        current_utc=current_utc,
        today_has_special_override=True,
        today_has_standard_configuration=True,
        intervals=intervals,
    )

    assert status == "HOLIDAY"
    assert all(interval.source == "special" for interval in intervals)


def test_phase2_closed_special_day_masks_overnight_standard_carryover() -> None:
    """A closed special date must also mask yesterday's overnight standard slot."""
    tz = ZoneInfo("America/New_York")
    special_date = date(2026, 1, 2)
    current_utc = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)  # 01:00 EST

    overnight_standard = _intervals_for_slots(
        date(2026, 1, 1),
        [MockBranchHours(open_time=time(22, 0), close_time=time(3, 0))],
        tz,
        source="standard",
    )
    intervals = _mask_standard_intervals_for_special_day(
        overnight_standard,
        special_date=special_date,
        tz=tz,
    )
    status, _, _ = _resolve_temporal_state(
        current_utc=current_utc,
        today_has_special_override=True,
        today_has_standard_configuration=False,
        intervals=intervals,
    )

    assert intervals == []
    assert status == "CLOSED"


def test_phase2_empty_state_resolution() -> None:
    status, next_open_at, next_close_at = _resolve_temporal_state(
        current_utc=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        today_has_special_override=False,
        today_has_standard_configuration=False,
        intervals=[],
    )

    assert status == "NOT_CONFIGURED"
    assert next_open_at is None
    assert next_close_at is None


def test_phase2_idempotent_source_hash_uses_schedule_source_only() -> None:
    data = {
        "timezone": "America/New_York",
        "weekly_schedule": {"0": []},
        "upcoming_exceptions": [],
        "source": "branch",
    }

    assert compute_source_hash(data) == compute_source_hash(data)


def test_phase2_canonical_json_sorting() -> None:
    data1 = {"b": 1, "a": 2}
    data2 = {"a": 2, "b": 1}

    assert compute_source_hash(data1) == compute_source_hash(data2)
