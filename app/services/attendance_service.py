import logging
from datetime import datetime, date, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError, SubscriptionNotActive
from app.models.attendance import AttendanceLog, CheckInMethod
from app.models.enums import SubscriptionStatus
from app.repositories.attendance_repo import AttendanceRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.services.member_service import MemberService

logger = logging.getLogger(__name__)


class AttendanceService:
    """Service for member check-in/out and attendance tracking."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.attendance_repo = AttendanceRepository(session)
        self.member_repo = MemberRepository(session)
        self.subscription_repo = SubscriptionRepository(session)

    async def check_access(self, member_uid: str) -> AttendanceLog:
        """
        Check member access via QR code scan.
        Creates attendance log with granted=true if subscription active.
        
        Args:
            member_uid: Member's unique UID from QR code
            
        Returns:
            AttendanceLog with access result
            
        Raises:
            NotFoundError: If member not found
            SubscriptionNotActive: If no active subscription
        """
        member = await self.member_repo.get_by_uid_active(member_uid)
        if not member:
            raise NotFoundError(f"Member with UID {member_uid} not found", error_code="NOT_FOUND")
        
        # Check active subscription
        active_sub = await self.subscription_repo.get_active_for_member(member.id, member.gym_id)
        if not active_sub:
            raise SubscriptionNotActive(f"Member {member.name} has no active subscription", error_code="SUBSCRIPTION_NOT_ACTIVE")
        
        # Create attendance log
        log = AttendanceLog(
            member_id=member.id,
            gym_id=member.gym_id,
            check_in_time=datetime.now(timezone.utc),
            method=CheckInMethod.QR,
            granted=True,
            notes="QR access granted"
        )
        
        created = await self.attendance_repo.create(log)
        await self.session.commit()
        
        # Update member's last_check_in
        member.last_check_in = datetime.now(timezone.utc)
        await self.member_repo.update(member)
        await self.session.commit()
        
        logger.info(f"QR access granted for member {member.name} at gym {member.gym_id}")
        return created

    async def manual_checkin(
        self,
        gym_id: UUID,
        member_id: UUID,
        method: CheckInMethod,
        staff_id: UUID
    ) -> AttendanceLog:
        """
        Manual check-in by staff.
        
        Args:
            gym_id: Gym UUID
            member_id: Member UUID
            method: Check-in method (MANUAL, CARD, etc.)
            staff_id: Staff UUID performing check-in
            
        Returns:
            Created AttendanceLog
            
        Raises:
            NotFoundError: If member not found in gym
            SubscriptionNotActive: If member has no active subscription
        """
        member = await self.member_repo.get_by_id_active(member_id, gym_id)
        if not member:
            raise NotFoundError(f"Member {member_id} not found in gym {gym_id}", error_code="NOT_FOUND")
        
        # Check active subscription
        active_sub = await self.subscription_repo.get_active_for_member(member_id, gym_id)
        if not active_sub:
            raise SubscriptionNotActive(f"Member {member.name} has no active subscription", error_code="SUBSCRIPTION_NOT_ACTIVE")
        
        # Check if already checked in (no checkout)
        open_log = await self.attendance_repo.get_active_checkin(member_id, gym_id)
        if open_log:
            raise ValidationError(
                f"Member {member.name} already checked in at {open_log.check_in_time}",
                error_code="VALIDATION_ERROR"
            )
        
        log = AttendanceLog(
            member_id=member_id,
            gym_id=gym_id,
            check_in_time=datetime.now(timezone.utc),
            method=method,
            granted=True,
            checked_in_by=staff_id,
            notes="Manual check-in by staff"
        )
        
        created = await self.attendance_repo.create(log)
        await self.session.commit()
        
        # Update member's last_check_in
        member.last_check_in = datetime.now(timezone.utc)
        await self.member_repo.update(member)
        await self.session.commit()
        
        logger.info(f"Manual check-in for member {member.name} by staff {staff_id}")
        return created

    async def checkout(
        self,
        gym_id: UUID,
        log_id: UUID
    ) -> AttendanceLog:
        """
        Record check-out time for an attendance log.
        
        Args:
            gym_id: Gym UUID (for scoping)
            log_id: Attendance log UUID
            
        Returns:
            Updated AttendanceLog
            
        Raises:
            NotFoundError: If log not found or already checked out
        """
        log = await self.attendance_repo.get_by_id(log_id, gym_id)
        if not log:
            raise NotFoundError(f"Attendance log {log_id} not found in gym {gym_id}", error_code="NOT_FOUND")
        
        if log.check_out_time:
            raise ValidationError(f"Already checked out at {log.check_out_time}", error_code="VALIDATION_ERROR")
        
        log.check_out_time = datetime.now(timezone.utc)
        updated = await self.attendance_repo.update(log)
        await self.session.commit()
        
        logger.info(f"Check-out recorded for log {log_id}")
        return updated

    async def list_logs(
        self,
        gym_id: UUID,
        date_filter: Optional[date] = None,
        member_id: Optional[UUID] = None,
        granted: Optional[bool] = None,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[AttendanceLog], int]:
        """
        List attendance logs with pagination and filters.
        
        Returns:
            Tuple of (list of logs, total count)
        """
        filters = {
            "date": date_filter,
            "member_id": member_id,
            "granted": granted
        }
        return await self.attendance_repo.list_paginated(gym_id, filters, page, size)

    async def member_history(
        self,
        member_id: UUID,
        gym_id: UUID,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[AttendanceLog], int]:
        """
        Get attendance history for a specific member.
        
        Args:
            member_id: Member UUID
            gym_id: Gym UUID (for scoping)
            page: Page number
            size: Items per page
            
        Returns:
            Tuple of (list of logs, total count)
            
        Raises:
            NotFoundError: If member not found in gym
        """
        # Verify member exists
        member = await self.member_repo.get_by_id_active(member_id, gym_id)
        if not member:
            raise NotFoundError(f"Member {member_id} not found in gym {gym_id}", error_code="NOT_FOUND")
        
        return await self.attendance_repo.member_history(member_id, gym_id, page, size)

    async def get_today_checkins(self, gym_id: UUID) -> List[AttendanceLog]:
        """Get all granted check-ins for today."""
        return await self.attendance_repo.get_today_checkins(gym_id)

    async def get_attendance_summary(
        self,
        gym_id: UUID,
        start_date: date,
        end_date: date
    ) -> dict:
        """
        Get attendance summary for reports.
        
        Returns:
            Dict with total_checkins, unique_members, average_daily
        """
        return await self.attendance_repo.get_attendance_summary(gym_id, start_date, end_date)

    async def get_attendance_heatmap(self, gym_id: UUID, days: int = 30) -> List[dict]:
        """
        Get attendance distribution by hour for last N days.
        """
        return await self.attendance_repo.get_attendance_heatmap(gym_id, days)