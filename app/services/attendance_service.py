import uuid
import json
from datetime import date
from fastapi import HTTPException
from app.models.attendance import AttendanceLog
from app.models.enums import CheckInMethod, AttendanceDenialReason, SubscriptionStatus, MemberStatus
from app.repositories.attendance_repo import AttendanceRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.schemas.attendance import AccessCheckResponse
from app.core.redis import redis_client

class AttendanceService:
    def __init__(self, attendance_repo: AttendanceRepository, member_repo: MemberRepository, sub_repo: SubscriptionRepository):
        self.attendance_repo = attendance_repo
        self.member_repo = member_repo
        self.sub_repo = sub_repo

    async def check_access(self, uid: str) -> AccessCheckResponse:
        # Rule 4: Access Check Logic
        # Step 1: Redis lookup
        cached = await redis_client.get(f"{uid}:access")
        if cached:
            return AccessCheckResponse(**json.loads(cached))

        # Step 2: DB lookup
        member = await self.member_repo.get_by_any_uid(uid)
        if not member:
            await self._log_denial(None, None, AttendanceDenialReason.not_found)
            raise HTTPException(403, {"granted": False, "reason": "not_found"})

        # Step 3: Subscription check
        sub = await self.sub_repo.get_active_for_member(member.id)
        if not sub:
            await self._log_denial(member.gym_id, member.id, AttendanceDenialReason.no_active_subscription)
            raise HTTPException(403, {"granted": False, "reason": "no_active_subscription"})

        if sub.status == SubscriptionStatus.frozen:
            await self._log_denial(member.gym_id, member.id, AttendanceDenialReason.account_frozen)
            raise HTTPException(403, {"granted": False, "reason": "account_frozen"})

        if sub.end_date < date.today():
            await self._log_denial(member.gym_id, member.id, AttendanceDenialReason.subscription_expired)
            raise HTTPException(403, {"granted": False, "reason": "subscription_expired"})

        # Step 4: Grant access
        result = {
            "granted": True, 
            "member_name": member.name, 
            "gym_id": str(member.gym_id), 
            "end_date": str(sub.end_date)
        }
        await redis_client.setex(f"{uid}:access", 43200, json.dumps(result))
        
        await self.attendance_repo.create(AttendanceLog(
            gym_id=member.gym_id,
            member_id=member.id,
            check_in_method=CheckInMethod.qr,
            access_granted=True
        ))
        
        return AccessCheckResponse(**result)

    async def _log_denial(self, gym_id, member_id, reason):
        if not gym_id: return
        await self.attendance_repo.create(AttendanceLog(
            gym_id=gym_id,
            member_id=member_id,
            check_in_method=CheckInMethod.qr,
            access_granted=False,
            denial_reason=reason
        ))
