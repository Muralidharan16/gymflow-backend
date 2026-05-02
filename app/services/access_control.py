import json
import logging
from datetime import datetime, date
from uuid import UUID
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.models import Device, Member, MemberSubscription, AttendanceLog, SubscriptionStatus
from ..redis_client import publish_channel
from ..services.whatsapp_service import send_whatsapp

logger = logging.getLogger(__name__)


async def verify_and_process_access(db: AsyncSession, redis, device_auth_token: str, device_id: str, fingerprint_id: str):
    """Verify access for a fingerprint, log, publish to redis, and send alerts if needed.

    Returns a dict with allowed boolean and details.
    """
    # Convert string UUID to UUID type
    try:
        device_id_uuid = UUID(device_id)
    except (ValueError, TypeError):
        # Invalid UUID format
        log = AttendanceLog(gym_id=None, member_id=None, scan_time=datetime.utcnow(), access_granted=False, denial_reason='device_error')
        db.add(log)
        await db.commit()
        raise PermissionError('invalid_device_id')

    # locate device by id and token
    q = select(Device).where(Device.id == device_id_uuid, Device.auth_token == device_auth_token)
    result = await db.execute(q)
    device = result.scalar_one_or_none()
    if device is None:
        raise PermissionError('device_not_registered')

    gym_id = device.gym_id

    # find member by fingerprint
    q = select(Member).where(Member.gym_id == gym_id, Member.fingerprint_id == fingerprint_id)
    result = await db.execute(q)
    member = result.scalar_one_or_none()

    if member is None:
        # unknown fingerprint -> log denial
        log = AttendanceLog(gym_id=gym_id, member_id=None, scan_time=datetime.utcnow(), access_granted=False, denial_reason='fingerprint_not_found')
        db.add(log)
        await db.commit()
        return { 'allowed': False, 'reason': 'fingerprint_not_found' }

    # check active subscription
    today = date.today()
    q = select(MemberSubscription).where(
        MemberSubscription.member_id == member.id,
        MemberSubscription.start_date <= today,
        MemberSubscription.end_date >= today,
        MemberSubscription.status == SubscriptionStatus.active
    ).order_by(desc(MemberSubscription.end_date)).limit(1)
    result = await db.execute(q)
    sub = result.scalar_one_or_none()

    if sub is None:
        # no active subscription -> deny and send WhatsApp
        log = AttendanceLog(gym_id=gym_id, member_id=member.id, scan_time=datetime.utcnow(), access_granted=False, denial_reason='no_subscription')
        db.add(log)
        await db.commit()

        # attempt to send WhatsApp asynchronously (fire-and-forget)
        try:
            await send_whatsapp(db, to_phone=member.phone, content=f"Dear {member.name}, your membership has expired. Please renew to gain access.", member_id=str(member.id), message_type='alert')
        except Exception as exc:
            logger.warning(f"WhatsApp send failed: {exc}")

        return { 'allowed': False, 'reason': 'no_subscription', 'member_id': str(member.id) }

    # allowed -> publish unlock command and log
    message = {
        'device_id': str(device.id),
        'action': 'unlock',
        'member_id': str(member.id),
        'member_name': member.name,
        'ts': datetime.utcnow().isoformat()
    }
    channel = f"tenant:{gym_id}:door_control"
    try:
        await publish_channel(channel, message)
    except Exception as exc:
        logger.warning(f"Redis publish failed: {exc}")

    log = AttendanceLog(gym_id=gym_id, member_id=member.id, scan_time=datetime.utcnow(), access_granted=True, denial_reason=None)
    db.add(log)
    await db.commit()

    return { 'allowed': True, 'member_id': str(member.id), 'member_name': member.name, 'subscription_end': str(sub.end_date) }
