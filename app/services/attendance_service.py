import uuid
import json
import logging
from datetime import date
from fastapi import HTTPException, status
from app.models.attendance import AttendanceLog
from app.models.enums import (
    CheckInMethod, AttendanceDenialReason,
    SubscriptionStatus
)
from app.repositories.attendance_repo import AttendanceRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.schemas.attendance import AccessCheckResponse
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

class AttendanceService:
    def __init__(
        self,
        attendance_repo: AttendanceRepository,
        member_repo: MemberRepository,
        sub_repo: SubscriptionRepository,
    ):
        self.attendance_repo = attendance_repo
        self.member_repo = member_repo
        self.sub_repo = sub_repo

    async def check_access(self, gym_id: uuid.UUID, uid: str) -> AccessCheckResponse:
        """
        Enterprise-grade access check.
        1. Redis Lookup (Fast Path)
        2. DB Lookup (Tenant-scoped)
        3. Subscription Validation
        4. Cache Warming
        """
        # Step 1: Redis lookup (scoping cache key by gym_id to prevent cross-tenant collisions)
        cache_key = f"access:{gym_id}:{uid}"
        cached = await redis_client.get(cache_key)
        if cached:
            return AccessCheckResponse(**json.loads(cached))

        # Step 2: DB lookup (strictly scoped to gym_id)
        member = await self.member_repo.get_by_any_uid(uid, gym_id)
        if not member:
            await self._log_denial(
                gym_id=gym_id,
                member_id=None,
                reason=AttendanceDenialReason.not_found,
                method=CheckInMethod.door_lock
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail={"granted": False, "reason": "not_found"}
            )

        # Step 3: Subscription check
        sub = await self.sub_repo.get_active_for_member(member.id, gym_id)
        if not sub:
            await self._log_denial(
                gym_id=gym_id,
                member_id=member.id,
                reason=AttendanceDenialReason.no_active_subscription,
                method=CheckInMethod.door_lock
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail={"granted": False, "reason": "no_active_subscription"}
            )

        if sub.status == SubscriptionStatus.frozen:
            await self._log_denial(
                gym_id=gym_id,
                member_id=member.id,
                reason=AttendanceDenialReason.account_frozen,
                method=CheckInMethod.door_lock
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail={"granted": False, "reason": "account_frozen"}
            )

        if sub.end_date < date.today():
            await self._log_denial(
                gym_id=gym_id,
                member_id=member.id,
                reason=AttendanceDenialReason.subscription_expired,
                method=CheckInMethod.door_lock
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail={"granted": False, "reason": "subscription_expired"}
            )

        # Step 4: Grant access & Warm Cache
        result_data = {
            "granted": True,
            "member_name": member.name,
            "gym_id": str(gym_id),
            "end_date": str(sub.end_date)
        }
        
        # Cache for 12 hours
        await redis_client.setex(cache_key, 43200, json.dumps(result_data))

        # Log successful attendance
        await self.attendance_repo.create(AttendanceLog(
            gym_id=gym_id,
            member_id=member.id,
            check_in_method=CheckInMethod.door_lock,
            access_granted=True
        ))
        
        return AccessCheckResponse(**result_data)

    async def _log_denial(
        self, 
        gym_id: uuid.UUID, 
        member_id: uuid.UUID | None,
        reason: AttendanceDenialReason, 
        method: CheckInMethod
    ) -> None:
        """Log access denial for audit and troubleshooting."""
        try:
            await self.attendance_repo.create(AttendanceLog(
                gym_id=gym_id,
                member_id=member_id,
                check_in_method=method,
                access_granted=False,
                denial_reason=reason
            ))
        except Exception as e:
            logger.error(f"Failed to log attendance denial: {e}")