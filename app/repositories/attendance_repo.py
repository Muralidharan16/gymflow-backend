import uuid
from sqlalchemy import select
from app.models.attendance import AttendanceLog
from app.repositories.base import BaseRepository

class AttendanceRepository(BaseRepository[AttendanceLog]):
    def __init__(self, session):
        super().__init__(AttendanceLog, session)
