import logging
import json
from datetime import datetime, date, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from ..models.models import Member, MemberSubscription, AttendanceLog, SubscriptionStatus, EntryType
from ..redis_client import redis_client

logger = logging.getLogger(__name__)


async def process_attendance_scan(
    db: AsyncSession,
    org_id: str,
    branch_id: str,
    fingerprint_id: str,
    device_id: str,
    scan_time: datetime = None
) -> dict:
    """
    High-speed attendance validation engine.
    
    Design decisions:
    - Uses Member.current_subscription_id + selectinload for a single indexed lookup.
    - Redis debounce (60s) prevents hardware spam from creating duplicate logs.
    - Returns member_name and subscription_end for the hardware display.
    - Fail-open on Redis: if Redis is down, debounce is skipped (attendance still works).
    """
    if not scan_time:
        scan_time = datetime.now(timezone.utc)
    
    today = date.today()
    debounce_key = f"attendance:debounce:{org_id}:{fingerprint_id}"

    # --- Redis debounce check ---
    if redis_client:
        try:
            cached = await redis_client.get(debounce_key)
            if cached:
                logger.debug(f"Debounced scan for FP={fingerprint_id}")
                try:
                    cached_data = json.loads(cached)
                except (json.JSONDecodeError, TypeError):
                    cached_data = {}
                return {
                    "access_granted": True,
                    "reason": "debounced",
                    "member_name": cached_data.get("member_name"),
                    "member_uid": cached_data.get("member_uid"),
                    "subscription_end": cached_data.get("subscription_end"),
                }
        except Exception as e:
            logger.error(f"Redis debounce check failed: {e}")

    # --- Single optimized DB query ---
    stmt = (
        select(Member)
        .options(selectinload(Member.current_subscription))
        .where(
            Member.org_id == org_id,
            Member.fingerprint_id == fingerprint_id,
            Member.deleted_at.is_(None),
        )
    )
    result = await db.execute(stmt)
    member = result.scalar_one_or_none()

    if not member:
        return {
            "access_granted": False,
            "reason": "member_not_found",
            "member_name": None,
            "member_uid": None,
            "subscription_end": None,
        }

    if not member.is_active:
        _log_attendance(db, member, org_id, branch_id, device_id, scan_time, False, "member_inactive")
        await db.commit()
        return {
            "access_granted": False,
            "reason": "member_inactive",
            "member_name": member.name,
            "member_uid": member.member_uid,
            "subscription_end": None,
        }

    sub = member.current_subscription
    if not sub:
        _log_attendance(db, member, org_id, branch_id, device_id, scan_time, False, "no_active_subscription")
        await db.commit()
        return {
            "access_granted": False,
            "reason": "no_active_subscription",
            "member_name": member.name,
            "member_uid": member.member_uid,
            "subscription_end": None,
        }

    # --- Subscription validation ---
    # end_date and grace_until are Date columns; compare with today (date), not datetime
    access_granted = False
    reason = ""
    sub_end = sub.end_date

    if sub.status == SubscriptionStatus.active:
        if sub.end_date and sub.end_date < today:
            if sub.grace_until and sub.grace_until >= today:
                access_granted = True
                reason = "grace_period_access"
            else:
                access_granted = False
                reason = "subscription_expired"
        else:
            access_granted = True
            reason = "active_subscription"
    elif sub.status == SubscriptionStatus.expired:
        # Might still be in grace
        if sub.grace_until and sub.grace_until >= today:
            access_granted = True
            reason = "grace_period_access"
        else:
            access_granted = False
            reason = "subscription_expired"
    else:
        access_granted = False
        reason = f"subscription_{sub.status.value}"

    _log_attendance(db, member, org_id, branch_id, device_id, scan_time, access_granted, reason)

    # --- Set debounce lock with member info for display ---
    if redis_client:
        try:
            debounce_data = json.dumps({
                "member_name": member.name,
                "member_uid": member.member_uid,
                "subscription_end": str(sub_end) if sub_end else None,
            })
            await redis_client.setex(debounce_key, 60, debounce_data)
        except Exception as e:
            logger.error(f"Redis debounce set failed: {e}")

    await db.commit()

    return {
        "access_granted": access_granted,
        "reason": reason,
        "member_name": member.name,
        "member_uid": member.member_uid,
        "subscription_end": str(sub_end) if sub_end else None,
    }


def _log_attendance(
    db: AsyncSession,
    member: Member,
    org_id: str,
    branch_id: str,
    device_id: str,
    scan_time: datetime,
    access_granted: bool,
    reason: str,
):
    """Creates an immutable attendance record with a point-in-time snapshot."""
    sub = member.current_subscription
    snapshot = json.dumps({
        "sub_status": sub.status.value if sub else None,
        "end_date": sub.end_date.isoformat() if sub and sub.end_date else None,
        "grace_until": sub.grace_until.isoformat() if sub and sub.grace_until else None,
        "reason": reason,
    })

    log = AttendanceLog(
        org_id=org_id,
        branch_id=branch_id,
        member_id=member.id,
        device_id=device_id,
        scan_time=scan_time,
        access_method="fingerprint",
        entry_type=EntryType.entry,
        access_granted=access_granted,
        denial_reason=reason if not access_granted else None,
        access_status_snapshot=snapshot,
    )
    db.add(log)
