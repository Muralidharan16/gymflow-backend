import uuid
from fastapi import APIRouter, Depends
from app.schemas.common import Response
from app.services.import_service import ImportService
from app.core.database import get_db

router = APIRouter(prefix="/gyms/{gym_id}/import", tags=["Imports"])

@router.get("/template")
async def get_template():
    service = ImportService(None)
    content = await service.get_template()
    return content
