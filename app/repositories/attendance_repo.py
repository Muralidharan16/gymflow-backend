from datetime import date, datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.attendance import AttendanceLog, CheckInMethod
from app.repositories.base_repo import BaseRepository


class AttendanceRepository(BaseRepository[AttendanceLog]):
    """Repository for AttendanceLog operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(AttendanceLog, session)

    async def get_by_id(self, log_id: UUID, gym_id: UUID) -> Optional[AttendanceLog]:
        """
        Get attendance log by ID, scoped to gym.
        
        Args:
            log_id: UUID of attendance log
            gym_id: Gym UUID for access control
            
        Returns:
            AttendanceLog if found and belongs to gym, else None
        """
        query = select(AttendanceLog).where(
            AttendanceLog.id == log_id,
            AttendanceLog.gym_id == gym_id
        ).options(selectinload(AttendanceLog.member))
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, log: AttendanceLog) -> AttendanceLog:
        """Create a new attendance log."""
        self.session.add(log)
        await self.session.flush()
        return log

    async def update(self, log: AttendanceLog) -> AttendanceLog:
        """Update an existing attendance log."""
        await self.session.merge(log)
        await self.session.flush()
        return log

    async def get_active_checkin(self, member_id: UUID, gym_id: UUID) -> Optional[AttendanceLog]:
        """
        Get the most recent check-in that hasn't been checked out.
        
        Args:
            member_id: Member UUID
            gym_id: Gym UUID
            
        Returns:
            AttendanceLog if open check-in exists, else None
        """
        query = select(AttendanceLog).where(
            AttendanceLog.member_id == member_id,
            AttendanceLog.gym_id == gym_id,
            AttendanceLog.check_out_time.is_(None),
            AttendanceLog.granted == True
        ).order_by(AttendanceLog.check_in_time.desc()).limit(1)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_paginated(
        self,
        gym_id: UUID,
        filters: dict,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[AttendanceLog], int]:
        """
        List attendance logs with pagination and filters.
        
        Args:
            gym_id: Gym UUID
            filters: Dictionary of filters (date, member_id, granted)
            page: Page number (1-indexed)
            size: Items per page
            
        Returns:
            Tuple of (list of logs, total count)
        """
        offset = (page - 1) * size
        
        # Base query
        query = select(AttendanceLog).where(AttendanceLog.gym_id == gym_id)
        
        # Apply filters
        if filters.get("date"):
            target_date = filters["date"]
            if isinstance(target_date, date):
                start = datetime.combine(target_date, datetime.min.time())
                end = datetime.combine(target_date, datetime.max.time())
                query = query.where(
                    AttendanceLog.check_in_time >= start,
                    AttendanceLog.check_in_time <= end
                )
        
        if filters.get("member_id"):
            query = query.where(AttendanceLog.member_id == filters["member_id"])
        
        if filters.get("granted") is not None:
            query = query.where(AttendanceLog.granted == filters["granted"])
        
        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        
        # Get paginated results with eager loading
        query = query.options(
            selectinload(AttendanceLog.member)
        ).order_by(AttendanceLog.check_in_time.desc()).offset(offset).limit(size)
        
        result = await self.session.execute(query)
        logs = result.scalars().all()
        
        return logs, total

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
            page: Page number (1-indexed)
            size: Items per page
            
        Returns:
            Tuple of (list of logs, total count)
        """
        offset = (page - 1) * size
        
        # Base query
        query = select(AttendanceLog).where(
            AttendanceLog.member_id == member_id,
            AttendanceLog.gym_id == gym_id
        )
        
        # Total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        
        # Paginated results
        query = query.order_by(AttendanceLog.check_in_time.desc()).offset(offset).limit(size)
        result = await self.session.execute(query)
        logs = result.scalars().all()
        
        return logs, total

    async def get_attendance_summary(
        self,
        gym_id: UUID,
        start_date: date,
        end_date: date
    ) -> dict:
        """
        Get attendance summary for a date range (for reports).
        
        Returns:
            Dictionary with keys: total_checkins, unique_members, average_daily
        """
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())
        
        # Total check-ins (granted only)
        total_query = select(func.count(AttendanceLog.id)).where(
            AttendanceLog.gym_id == gym_id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= start_dt,
            AttendanceLog.check_in_time <= end_dt
        )
        total_result = await self.session.execute(total_query)
        total_checkins = total_result.scalar() or 0
        
        # Unique members
        unique_query = select(func.count(AttendanceLog.member_id.distinct())).where(
            AttendanceLog.gym_id == gym_id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= start_dt,
            AttendanceLog.check_in_time <= end_dt
        )
        unique_result = await self.session.execute(unique_query)
        unique_members = unique_result.scalar() or 0
        
        # Average daily (days between inclusive)
        days = (end_date - start_date).days + 1
        avg_daily = total_checkins / days if days > 0 else 0
        
        return {
            "total_checkins": total_checkins,
            "unique_members": unique_members,
            "average_daily": round(avg_daily, 2),
            "days_in_range": days
        }

    async def get_attendance_heatmap(self, gym_id: UUID, days: int = 30) -> List[dict]:
        """
        Get attendance distribution by hour for last N days.
        
        Args:
            gym_id: Gym UUID
            days: Number of days to look back
            
        Returns:
            List of dicts with hour and count
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(days=days)
        
        # PostgreSQL extract hour
        query = select(
            func.extract('hour', AttendanceLog.check_in_time).label('hour'),
            func.count().label('count')
        ).where(
            AttendanceLog.gym_id == gym_id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= cutoff
        ).group_by('hour').order_by('hour')
        
        result = await self.session.execute(query)
        rows = result.all()
        
        return [{"hour": int(row.hour), "count": row.count} for row in rows]

    async def get_today_checkins(self, gym_id: UUID) -> List[AttendanceLog]:
        """Get all granted check-ins for today."""
        today = date.today()
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
        
        query = select(AttendanceLog).where(
            AttendanceLog.gym_id == gym_id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= start,
            AttendanceLog.check_in_time <= end
        ).options(selectinload(AttendanceLog.member))
        
        result = await self.session.execute(query)
        return result.scalars().all()