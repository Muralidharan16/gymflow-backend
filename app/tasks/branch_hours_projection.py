import uuid
import json
import hashlib
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, or_, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
import asyncio
from celery import shared_task
from app.core.database import async_session_maker

from app.models.org_branch import OrgBranch
from app.models.branch_operating_hours import (
    BranchOperatingHours,
    OrganizationOperatingHours,
    BranchSpecialHours,
    BranchHoursProjection
)

def compute_source_hash(data: dict) -> str:
    """Canonicalize JSON payload and hash it to determine if projection changed."""
    canonical_json = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()

def resolve_current_status(
    branch_tz: ZoneInfo,
    current_utc: datetime,
    today_special: List[BranchSpecialHours],
    today_standard: List[Any],
) -> tuple[str, Optional[datetime], Optional[datetime]]:
    """
    Resolve 'OPEN', 'CLOSED', 'HOLIDAY', 'NOT_CONFIGURED'.
    Returns (status, next_open_at, next_close_at)
    """
    if not today_special and not today_standard:
        return 'NOT_CONFIGURED', None, None
    
    current_local = current_utc.astimezone(branch_tz)
    current_time = current_local.time()
    
    # HOLIDAY overrides STANDARD
    if today_special:
        if any(s.is_closed for s in today_special):
            return 'CLOSED', None, None # Actually status = HOLIDAY if it's a special day? Spec says HOLIDAY overrides OPEN.
            
        # Check if currently inside any special hours slot
        for slot in today_special:
            if slot.is_24_hours:
                return 'HOLIDAY', None, None
            if slot.open_time and slot.close_time:
                if slot.is_overnight:
                    if current_time >= slot.open_time or current_time <= slot.close_time:
                        return 'HOLIDAY', None, None
                else:
                    if slot.open_time <= current_time <= slot.close_time:
                        return 'HOLIDAY', None, None
                        
        return 'CLOSED', None, None
        
    # STANDARD rules
    for slot in today_standard:
        if slot.is_closed:
            continue
        if slot.is_24_hours:
            return 'OPEN', None, None
        if slot.open_time and slot.close_time:
            if slot.is_overnight:
                if current_time >= slot.open_time or current_time <= slot.close_time:
                    return 'OPEN', None, None
            else:
                if slot.open_time <= current_time <= slot.close_time:
                    return 'OPEN', None, None
                    
    return 'CLOSED', None, None


async def rebuild_branch_hours_projection(db: AsyncSession, branch_id: uuid.UUID):
    """
    CQRS Worker: Rebuilds the branch_hours_projection for a given branch_id.
    """
    # 1. Fetch branch timezone and org_id
    stmt = select(OrgBranch.org_id, OrgBranch.timezone).where(OrgBranch.id == branch_id)
    result = await db.execute(stmt)
    branch_info = result.first()
    if not branch_info:
        return # Branch doesn't exist

    org_id, tz_string = branch_info
    tz = ZoneInfo(tz_string)
    current_utc = datetime.now(timezone.utc)
    current_local = current_utc.astimezone(tz)
    today_date = current_local.date()
    
    # 2. Fetch Branch Hours
    branch_stmt = select(BranchOperatingHours).where(
        BranchOperatingHours.branch_id == branch_id,
        BranchOperatingHours.deleted_at.is_(None),
        BranchOperatingHours.valid_from <= today_date,
        or_(BranchOperatingHours.valid_until.is_(None), BranchOperatingHours.valid_until >= today_date)
    ).order_by(BranchOperatingHours.day_of_week, BranchOperatingHours.open_time)
    
    branch_hours = (await db.scalars(branch_stmt)).all()
    
    # 3. Fallback to Org Hours if no branch hours
    standard_hours = branch_hours
    if not branch_hours:
        org_stmt = select(OrganizationOperatingHours).where(
            OrganizationOperatingHours.org_id == org_id,
            OrganizationOperatingHours.deleted_at.is_(None),
            OrganizationOperatingHours.valid_from <= today_date,
            or_(OrganizationOperatingHours.valid_until.is_(None), OrganizationOperatingHours.valid_until >= today_date)
        ).order_by(OrganizationOperatingHours.day_of_week, OrganizationOperatingHours.open_time)
        standard_hours = (await db.scalars(org_stmt)).all()

    # 4. Fetch Special Hours (next 30 days for upcoming exceptions)
    special_stmt = select(BranchSpecialHours).where(
        BranchSpecialHours.branch_id == branch_id,
        BranchSpecialHours.deleted_at.is_(None),
        BranchSpecialHours.special_date >= today_date,
        BranchSpecialHours.special_date <= today_date + timedelta(days=30)
    ).order_by(BranchSpecialHours.special_date, BranchSpecialHours.open_time)
    
    special_hours = (await db.scalars(special_stmt)).all()

    # Create canonical weekly_schedule JSON
    weekly_schedule = {str(d): [] for d in range(7)}
    for slot in standard_hours:
        weekly_schedule[str(slot.day_of_week)].append({
            "is_closed": slot.is_closed,
            "is_24_hours": slot.is_24_hours,
            "open_time": slot.open_time.isoformat() if slot.open_time else None,
            "close_time": slot.close_time.isoformat() if slot.close_time else None
        })

    # Create canonical upcoming_exceptions JSON
    upcoming_exceptions = []
    for sp in special_hours:
        upcoming_exceptions.append({
            "date": sp.special_date.isoformat(),
            "reason": sp.reason,
            "is_closed": sp.is_closed,
            "is_24_hours": sp.is_24_hours,
            "open_time": sp.open_time.isoformat() if sp.open_time else None,
            "close_time": sp.close_time.isoformat() if sp.close_time else None
        })

    # Determine Status
    today_special = [s for s in special_hours if s.special_date == today_date]
    today_standard = [s for s in standard_hours if s.day_of_week == current_local.weekday()]
    
    status, next_open_at, next_close_at = resolve_current_status(
        tz, current_utc, today_special, today_standard
    )
    
    # Calculate DST safe next_open_at / next_close_at
    def get_datetime(t_date: date, t_time: time) -> datetime:
        local_dt = datetime.combine(t_date, t_time, tzinfo=tz)
        return local_dt.astimezone(timezone.utc)
        
    next_open_at = None
    next_close_at = None
    
    # Very simple forward scan for next 7 days
    # In a real heavy production system, you'd scan the canonical arrays built above.
    for i in range(8):
        scan_date = today_date + timedelta(days=i)
        
        # Check special hours first
        sp_for_day = [s for s in special_hours if s.special_date == scan_date]
        if sp_for_day:
            for sp in sp_for_day:
                if sp.is_closed or sp.is_24_hours: continue
                if sp.open_time and not next_open_at:
                    cand = get_datetime(scan_date, sp.open_time)
                    if cand > current_utc: next_open_at = cand
                if sp.close_time and not next_close_at:
                    cand = get_datetime(scan_date + timedelta(days=1) if sp.is_overnight else scan_date, sp.close_time)
                    if cand > current_utc: next_close_at = cand
        else:
            # Check standard hours
            st_for_day = [s for s in standard_hours if s.day_of_week == scan_date.weekday()]
            for st in st_for_day:
                if st.is_closed or st.is_24_hours: continue
                if st.open_time and not next_open_at:
                    cand = get_datetime(scan_date, st.open_time)
                    if cand > current_utc: next_open_at = cand
                if st.close_time and not next_close_at:
                    cand = get_datetime(scan_date + timedelta(days=1) if st.is_overnight else scan_date, st.close_time)
                    if cand > current_utc: next_close_at = cand
                    
        if next_open_at and next_close_at:
            break
    
    projection_data = {
        "timezone": tz_string,
        "current_status": status,
        "weekly_schedule": weekly_schedule,
        "upcoming_exceptions": upcoming_exceptions
    }
    
    new_hash = compute_source_hash(projection_data)
    
    # Upsert
    stmt = insert(BranchHoursProjection).values(
        branch_id=branch_id,
        projection_version=1,
        last_rebuilt_at=datetime.now(timezone.utc),
        source_hash=new_hash,
        timezone=tz_string,
        current_status=status,
        next_open_at=next_open_at,
        next_close_at=next_close_at,
        weekly_schedule=weekly_schedule,
        upcoming_exceptions=upcoming_exceptions
    )
    
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=['branch_id'],
        set_=dict(
            projection_version=BranchHoursProjection.projection_version + (
                1 if stmt.excluded.source_hash != BranchHoursProjection.source_hash else 0
            ),
            last_rebuilt_at=stmt.excluded.last_rebuilt_at,
            source_hash=stmt.excluded.source_hash,
            timezone=stmt.excluded.timezone,
            current_status=stmt.excluded.current_status,
            next_open_at=stmt.excluded.next_open_at,
            next_close_at=stmt.excluded.next_close_at,
            weekly_schedule=stmt.excluded.weekly_schedule,
            upcoming_exceptions=stmt.excluded.upcoming_exceptions
        ),
        where=(BranchHoursProjection.source_hash != stmt.excluded.source_hash)
    )
    
    await db.execute(upsert_stmt)
    await db.commit()

@shared_task(name="app.tasks.branch_hours_projection.run_projection")
def run_projection(branch_id: str):
    """
    Celery task wrapper to trigger the projection rebuild asynchronously.
    """
    async def _runner():
        async with async_session_maker() as db:
            await rebuild_branch_hours_projection(db, uuid.UUID(branch_id))
            
    asyncio.run(_runner())
