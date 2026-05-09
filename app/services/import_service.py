# app/services/import_service.py (Stub as spec is complex for CSV)
import uuid
from app.repositories.base import BaseRepository

class ImportService:
    def __init__(self, session):
        self.session = session
    
    async def get_template(self):
        return "member_name,phone,email,dob,gender\nJohn Doe,9876543210,john@example.com,1990-01-01,male"
