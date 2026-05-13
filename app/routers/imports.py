import csv
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.core.deps import Staff
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.imports import (
    ImportPreviewResponse, ImportConfirmRequest, ImportLogResponse
)
from app.services.import_service import ImportService
from app.core.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/gyms/{gym_id}/import", tags=["Import"])


@router.get("/template")
async def download_template(
    gym_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Download CSV template file for member import.
    """
    service = ImportService(db)
    csv_content = await service.get_template_csv()
    
    # Streaming response as CSV file
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=member_import_template.csv"}
    )


@router.post("/preview", response_model=Response[ImportPreviewResponse])
async def preview_import(
    gym_id: UUID,
    file: UploadFile = File(..., description="CSV file to import"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Upload CSV file, preview columns and rows, store temporary data in Redis.
    Returns import_id for later confirmation.
    """
    # Validate file type
    if not file.filename.endswith('.csv'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Only CSV files are allowed", "error_code": "VALIDATION_ERROR"}
        )
    
    # Read file content
    content = await file.read()
    
    service = ImportService(db)
    try:
        preview = await service.create_preview(gym_id, content, file.filename, current_staff.id)
        await db.commit()
        return Response(data=ImportPreviewResponse(**preview))
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": f"Failed to process file: {str(e)}", "error_code": "INTERNAL_ERROR"}
        )


@router.post("/confirm/{import_id}", response_model=Response[dict])
async def confirm_import(
    gym_id: UUID,
    import_id: str,
    data: ImportConfirmRequest,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Execute bulk member import using stored preview data and column mapping.
    """
    service = ImportService(db)
    try:
        result = await service.confirm_import(
            import_id=import_id,
            gym_id=gym_id,
            column_mapping=data.column_mapping,
            created_by=current_staff.id
        )
        await db.commit()
        return Response(data=result)
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.get("/logs", response_model=PaginatedResponse[ImportLogResponse])
async def get_import_logs(
    gym_id: UUID,
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get paginated import history for this gym.
    """
    service = ImportService(db)
    logs, total = await service.get_import_logs(gym_id, page, size)
    return PaginatedResponse(
        data=[ImportLogResponse.model_validate(log) for log in logs],
        page=page,
        size=size,
        total=total
    )


@router.get("/logs/{import_id}/errors")
async def download_import_errors(
    gym_id: UUID,
    import_id: str,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Download CSV file containing error rows for a specific import.
    """
    service = ImportService(db)
    try:
        csv_content = await service.get_import_errors(import_id, gym_id)
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=import_errors_{import_id}.csv"}
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )