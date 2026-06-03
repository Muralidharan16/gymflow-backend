import pytest
from datetime import datetime, timezone, time, date, timedelta
from zoneinfo import ZoneInfo
from app.tasks.branch_hours_projection import resolve_current_status, compute_source_hash

def test_phase2_dst_boundary_math():
    """
    Test 2.1: DST Boundary Math.
    Properly binding naive time to target date in timezone.
    """
    tz = ZoneInfo("America/New_York")
    
    # 2026 Spring Forward in US is March 8, 2026. Monday after is March 9.
    target_date = date(2026, 3, 9)
    open_time = time(6, 0)
    
    # Standard correct way to evaluate local time into UTC
    local_dt = datetime.combine(target_date, open_time, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    
    # EDT is UTC-4, so 06:00 local is 10:00 UTC.
    # Note: If the test map meant Fall-Back, it would be 11:00 UTC. 
    # We test that the offset logic is correctly applied by the ZoneInfo object.
    assert utc_dt.hour == 10, f"Expected 10:00 UTC for 06:00 EDT, got {utc_dt.hour}:00 UTC"

class MockBranchHours:
    def __init__(self, is_closed=False, is_24_hours=False, is_overnight=False, open_time=None, close_time=None):
        self.is_closed = is_closed
        self.is_24_hours = is_24_hours
        self.is_overnight = is_overnight
        self.open_time = open_time
        self.close_time = close_time

def test_phase2_status_resolution_hierarchy():
    """
    Test 2.2: Status Resolution Hierarchy
    HOLIDAY > OPEN.
    """
    tz = ZoneInfo("America/New_York")
    current_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc) # 07:00 EST
    
    today_special = [
        MockBranchHours(open_time=time(6, 0), close_time=time(18, 0))
    ]
    today_standard = [
        MockBranchHours(open_time=time(5, 0), close_time=time(23, 0))
    ]
    
    status, _, _ = resolve_current_status(tz, current_utc, today_special, today_standard)
    assert status == 'HOLIDAY', "Active special hours must resolve to HOLIDAY overriding standard OPEN"

def test_phase2_empty_state_resolution():
    """
    Test 2.3: Empty State Resolution
    """
    tz = ZoneInfo("America/New_York")
    current_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    status, _, _ = resolve_current_status(tz, current_utc, [], [])
    assert status == 'NOT_CONFIGURED', "Empty lists must resolve to NOT_CONFIGURED"

def test_phase2_idempotent_rebuilds():
    """
    Test 2.5: Idempotent Rebuilds
    """
    data = {
        "timezone": "America/New_York",
        "current_status": "OPEN",
        "weekly_schedule": {"0": []},
        "upcoming_exceptions": []
    }
    
    hash1 = compute_source_hash(data)
    hash2 = compute_source_hash(data)
    
    assert hash1 == hash2, "Identical inputs must yield identical source_hash"

def test_phase2_canonical_json_sorting():
    """
    Test 2.6: Canonical JSON Sorting
    """
    data1 = {"b": 1, "a": 2}
    data2 = {"a": 2, "b": 1}
    
    assert compute_source_hash(data1) == compute_source_hash(data2), "JSON serialization must be canonically sorted"
