import uuid
import logging
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, Form, Response as FastApiResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.deps import get_db, require_org_admin, get_current_active_staff, Staff
from app.models.organization import Organization, OrganizationAssetAudit
from app.schemas.organization import (
    AssetUploadUrlResponse,
    LogoConfirmRequest,
    CoverConfirmRequest,
    LogoStatusResponse,
    CoverStatusResponse
)
from app.schemas.common import Response
from app.utils.s3 import get_s3_client
from app.core.config import settings
from app.tasks.logos import process_org_logo, delete_old_s3_assets
from app.tasks.covers import process_org_cover
from app.core.celery_app import celery_app

logger = logging.getLogger("doers")

# Core configurations synchronized per asset type
ASSET_CONFIGS = {
    "logo": {
        "max_size_bytes": 5_242_880,  # 5MB
        "min_width": 200,
        "min_height": 200,
        "confirm_schema": LogoConfirmRequest,
        "status_schema": LogoStatusResponse,
        "task": process_org_logo,
        "db_keys": ["logo_key", "logo_thumb_key", "logo_medium_key", "logo_full_key"]
    },
    "cover": {
        "max_size_bytes": 10_485_760,  # 10MB
        "min_width": 1200,
        "min_height": 400,
        "confirm_schema": CoverConfirmRequest,
        "status_schema": CoverStatusResponse,
        "task": process_org_cover,
        "db_keys": ["cover_key", "cover_mobile_key", "cover_tablet_key", "cover_desktop_key"]
    }
}



# To comply with FastAPI validation and OpenAPI generation, we define the endpoints explicitly using a clean, non-duplicated pattern
def register_asset_endpoints(router: APIRouter, asset_type: Literal["logo", "cover"]):
    config = ASSET_CONFIGS[asset_type]

    @router.post(f"/{asset_type}/upload-url", response_model=Response[AssetUploadUrlResponse])
    async def get_upload_url(
        current_staff: Staff = Depends(require_org_admin)
    ):
        try:
            s3 = get_s3_client()
            upload_id = uuid.uuid4().hex
            
            conditions = [
                ["content-length-range", 1024, config["max_size_bytes"]],
                ["starts-with", "$Content-Type", "image/"],
                {"bucket": settings.S3_BUCKET_NAME},
                {"key": f"quarantine/{upload_id}"}
            ]
            
            response = s3.generate_presigned_post(
                Bucket=settings.S3_BUCKET_NAME,
                Key=f"quarantine/{upload_id}",
                Fields={"Content-Type": "image/png"},
                Conditions=conditions,
                ExpiresIn=300
            )
            
            return Response(data=AssetUploadUrlResponse(
                upload_url=response['url'],
                fields=response['fields'],
                upload_id=upload_id,
                expires_in=300
            ))
        except Exception as e:
            logger.error(f"Failed to generate upload URL for {asset_type}: {str(e)}")
            raise HTTPException(status_code=500, detail="Upload URL generation failed.")

    if asset_type == "logo":
        @router.patch("/logo/confirm")
        async def confirm_logo_upload(
            request: Request,
            data: LogoConfirmRequest,
            current_staff: Staff = Depends(require_org_admin)
        ):
            try:
                request_ip = request.client.host if request.client else None
                process_org_logo.delay(str(current_staff.org_id), data.upload_id, str(current_staff.id), request_ip)
                return Response(data={"status": "processing"})
            except Exception as e:
                logger.exception("Logo confirm failed")
                raise HTTPException(status_code=500, detail="Action failed. Please try again.")

        @router.get("/logo/status", response_model=Response[LogoStatusResponse])
        async def get_logo_status(
            current_staff: Staff = Depends(get_current_active_staff),
            db: AsyncSession = Depends(get_db)
        ):
            q = select(Organization).where(Organization.id == current_staff.org_id)
            res = await db.execute(q)
            org = res.scalar_one_or_none()
            if not org:
                raise HTTPException(status_code=404, detail="Organization not found")
                
            return Response(data=LogoStatusResponse(
                status=org.logo_status.value if org.logo_status else "null",
                logo_thumb_url=f"{settings.CDN_BASE_URL}/{org.logo_thumb_key}" if org.logo_thumb_key else None,
                logo_medium_url=f"{settings.CDN_BASE_URL}/{org.logo_medium_key}" if org.logo_medium_key else None,
                logo_full_url=f"{settings.CDN_BASE_URL}/{org.logo_full_key}" if org.logo_full_key else None,
            ))
    else:
        @router.patch("/cover/confirm")
        async def confirm_cover_upload(
            request: Request,
            data: CoverConfirmRequest,
            current_staff: Staff = Depends(require_org_admin)
        ):
            try:
                request_ip = request.client.host if request.client else None
                process_org_cover.delay(str(current_staff.org_id), data.upload_id, str(current_staff.id), request_ip, data.focal_y)
                return Response(data={"status": "processing"})
            except Exception as e:
                logger.exception("Cover confirm failed")
                raise HTTPException(status_code=500, detail="Action failed. Please try again.")

        @router.get("/cover/status", response_model=Response[CoverStatusResponse])
        async def get_cover_status(
            current_staff: Staff = Depends(get_current_active_staff),
            db: AsyncSession = Depends(get_db)
        ):
            q = select(Organization).where(Organization.id == current_staff.org_id)
            res = await db.execute(q)
            org = res.scalar_one_or_none()
            if not org:
                raise HTTPException(status_code=404, detail="Organization not found")
                
            return Response(data=CoverStatusResponse(
                status=org.cover_status.value if org.cover_status else "null",
                cover_mobile_url=f"{settings.CDN_BASE_URL}/{org.cover_mobile_key}" if org.cover_mobile_key else None,
                cover_tablet_url=f"{settings.CDN_BASE_URL}/{org.cover_tablet_key}" if org.cover_tablet_key else None,
                cover_desktop_url=f"{settings.CDN_BASE_URL}/{org.cover_desktop_key}" if org.cover_desktop_key else None,
            ))

    @router.delete(f"/{asset_type}")
    async def delete_asset(
        current_staff: Staff = Depends(require_org_admin),
        db: AsyncSession = Depends(get_db)
    ):
        try:
            q = select(Organization).where(Organization.id == current_staff.org_id)
            res = await db.execute(q)
            org = res.scalar_one_or_none()
            
            primary_key = getattr(org, config["db_keys"][0]) if org else None
            if not org or not primary_key:
                return Response(data={"message": f"{asset_type.capitalize()} deleted successfully"})
                
            old_keys = []
            for col in config["db_keys"]:
                val = getattr(org, col)
                if val:
                    old_keys.append(val)
            
            audit = OrganizationAssetAudit(
                org_id=current_staff.org_id,
                changed_by=current_staff.id,
                asset_type=asset_type,
                action="deleted",
                old_s3_key=primary_key
            )
            
            for col in config["db_keys"]:
                setattr(org, col, None)
            
            if asset_type == "logo":
                org.logo_meta = None
                org.logo_status = None
            else:
                org.cover_meta = None
                org.cover_status = None
            
            db.add(audit)
            await db.commit()
            
            if old_keys:
                delete_old_s3_assets.delay(old_keys)
                
            return Response(data={"message": f"{asset_type.capitalize()} deleted successfully"})
        except Exception as e:
            logger.error(f"Failed to delete {asset_type}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to delete {asset_type}.")

# Expose a centralized router
router = APIRouter(prefix="/organizations", tags=["Branding Assets"])
register_asset_endpoints(router, "logo")
register_asset_endpoints(router, "cover")


@router.post("/mock-s3/upload")
async def mock_s3_upload(
    file: UploadFile,
    key: str = Form(...)
):
    """
    Simulation endpoint capturing client browser uploads in local development.
    Writes payload straight to our local file system Mock S3.
    """
    try:
        s3 = get_s3_client()
        content = await file.read()
        s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=content)
        return FastApiResponse(status_code=204)
    except Exception as e:
        logger.error(f"Local simulated S3 upload failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Local mock upload failed")
