import logging
import uuid
from typing import Literal

import sqlalchemy as sa
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response as FastApiResponse,
    UploadFile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import Staff, get_db, require_org_admin
from app.schemas.common import Response
from app.schemas.organization import (
    AssetUploadUrlResponse,
    CoverConfirmRequest,
    CoverStatusResponse,
    LogoConfirmRequest,
    LogoStatusResponse,
)
from app.tasks.logos import process_organization_asset
from app.utils.s3 import get_s3_client


logger = logging.getLogger("doers")

ASSET_CONFIGS = {
    "logo": {
        "max_size_bytes": 5_242_880,
        "min_width": 200,
        "min_height": 200,
    },
    "cover": {
        "max_size_bytes": 10_485_760,
        "min_width": 1200,
        "min_height": 400,
    },
}


def _require_owner_principal(request: Request) -> None:
    # The current schema has a durable Owner identity but no durable org-level
    # admin role record for organization_user. Do not turn a queued JWT role
    # claim into long-lived S3 mutation authority.
    if getattr(request.state, "principal_type", None) != "owner":
        raise HTTPException(
            status_code=403,
            detail="Organization owner authorization is required for branding changes.",
        )


async def _enqueue_asset_job(
    db: AsyncSession,
    *,
    asset_type: str,
    upload_id: str,
    focal_y: float | None,
    request_ip: str | None,
) -> uuid.UUID:
    job_id = await db.scalar(
        sa.text(
            """
            SELECT app_secure.enqueue_organization_asset_job(
                :asset_type, :upload_id, :focal_y, :request_ip
            )
            """
        ),
        {
            "asset_type": asset_type,
            "upload_id": upload_id,
            "focal_y": focal_y,
            "request_ip": request_ip,
        },
    )
    if job_id is None:
        raise RuntimeError("Asset enqueue capability returned no job identifier")
    # Durability precedes broker publication. The maintenance dispatcher repairs
    # a broker outage without needing the client to repeat an accepted command.
    await db.commit()
    return job_id


async def _current_profile(db: AsyncSession) -> dict:
    row = (
        await db.execute(
            sa.text(
                """
                SELECT logo_status, logo_thumb_key, logo_medium_key, logo_full_key,
                       cover_status, cover_mobile_key, cover_tablet_key,
                       cover_desktop_key
                FROM app_secure.current_organization_profile()
                """
            )
        )
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return dict(row)


def register_asset_endpoints(
    router: APIRouter,
    asset_type: Literal["logo", "cover"],
) -> None:
    config = ASSET_CONFIGS[asset_type]

    @router.post(
        f"/{asset_type}/upload-url",
        response_model=Response[AssetUploadUrlResponse],
    )
    async def get_upload_url(
        current_staff: Staff = Depends(require_org_admin),
    ):
        try:
            s3 = get_s3_client()
            upload_id = uuid.uuid4().hex
            quarantine_key = f"quarantine/{current_staff.org_id}/{upload_id}"
            conditions = [
                ["content-length-range", 1024, config["max_size_bytes"]],
                ["starts-with", "$Content-Type", "image/"],
                {"bucket": settings.S3_BUCKET_NAME},
                {"key": quarantine_key},
            ]
            response = s3.generate_presigned_post(
                Bucket=settings.S3_BUCKET_NAME,
                Key=quarantine_key,
                Fields={"Content-Type": "image/png"},
                Conditions=conditions,
                ExpiresIn=300,
            )
            return Response(
                data=AssetUploadUrlResponse(
                    upload_url=response["url"],
                    fields=response["fields"],
                    upload_id=upload_id,
                    expires_in=300,
                )
            )
        except Exception as exc:
            logger.error(
                "Failed to generate upload URL for %s: %s",
                asset_type,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail="Upload URL generation failed.",
            ) from exc

    if asset_type == "logo":

        @router.patch("/logo/confirm")
        async def confirm_logo_upload(
            request: Request,
            data: LogoConfirmRequest,
            current_staff: Staff = Depends(require_org_admin),
            db: AsyncSession = Depends(get_db),
        ):
            _require_owner_principal(request)
            try:
                request_ip = request.client.host if request.client else None
                job_id = await _enqueue_asset_job(
                    db,
                    asset_type="logo",
                    upload_id=data.upload_id,
                    focal_y=None,
                    request_ip=request_ip,
                )
                try:
                    process_organization_asset.delay(str(job_id))
                except Exception:
                    logger.exception(
                        "Asset job %s committed but initial broker publish failed; "
                        "maintenance redispatch will recover it",
                        job_id,
                    )
                return Response(data={"status": "processing"})
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Logo confirm failed")
                raise HTTPException(
                    status_code=500,
                    detail="Action failed. Please try again.",
                ) from exc

        @router.get(
            "/logo/status",
            response_model=Response[LogoStatusResponse],
        )
        async def get_logo_status(
            _current_staff: Staff = Depends(require_org_admin),
            db: AsyncSession = Depends(get_db),
        ):
            profile = await _current_profile(db)
            return Response(
                data=LogoStatusResponse(
                    status=profile["logo_status"] or "null",
                    logo_thumb_url=(
                        f"{settings.CDN_BASE_URL}/{profile['logo_thumb_key']}"
                        if profile["logo_thumb_key"]
                        else None
                    ),
                    logo_medium_url=(
                        f"{settings.CDN_BASE_URL}/{profile['logo_medium_key']}"
                        if profile["logo_medium_key"]
                        else None
                    ),
                    logo_full_url=(
                        f"{settings.CDN_BASE_URL}/{profile['logo_full_key']}"
                        if profile["logo_full_key"]
                        else None
                    ),
                )
            )
    else:

        @router.patch("/cover/confirm")
        async def confirm_cover_upload(
            request: Request,
            data: CoverConfirmRequest,
            current_staff: Staff = Depends(require_org_admin),
            db: AsyncSession = Depends(get_db),
        ):
            _require_owner_principal(request)
            try:
                request_ip = request.client.host if request.client else None
                job_id = await _enqueue_asset_job(
                    db,
                    asset_type="cover",
                    upload_id=data.upload_id,
                    focal_y=data.focal_y,
                    request_ip=request_ip,
                )
                try:
                    process_organization_asset.delay(str(job_id))
                except Exception:
                    logger.exception(
                        "Asset job %s committed but initial broker publish failed; "
                        "maintenance redispatch will recover it",
                        job_id,
                    )
                return Response(data={"status": "processing"})
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Cover confirm failed")
                raise HTTPException(
                    status_code=500,
                    detail="Action failed. Please try again.",
                ) from exc

        @router.get(
            "/cover/status",
            response_model=Response[CoverStatusResponse],
        )
        async def get_cover_status(
            _current_staff: Staff = Depends(require_org_admin),
            db: AsyncSession = Depends(get_db),
        ):
            profile = await _current_profile(db)
            return Response(
                data=CoverStatusResponse(
                    status=profile["cover_status"] or "null",
                    cover_mobile_url=(
                        f"{settings.CDN_BASE_URL}/{profile['cover_mobile_key']}"
                        if profile["cover_mobile_key"]
                        else None
                    ),
                    cover_tablet_url=(
                        f"{settings.CDN_BASE_URL}/{profile['cover_tablet_key']}"
                        if profile["cover_tablet_key"]
                        else None
                    ),
                    cover_desktop_url=(
                        f"{settings.CDN_BASE_URL}/{profile['cover_desktop_key']}"
                        if profile["cover_desktop_key"]
                        else None
                    ),
                )
            )

    @router.delete(f"/{asset_type}")
    async def delete_asset(
        request: Request,
        _current_staff: Staff = Depends(require_org_admin),
        db: AsyncSession = Depends(get_db),
    ):
        _require_owner_principal(request)
        try:
            await db.execute(
                sa.text(
                    "SELECT app_secure.delete_current_organization_asset(:asset_type)"
                ),
                {"asset_type": asset_type},
            )
            await db.commit()
            return Response(
                data={"message": f"{asset_type.capitalize()} deleted successfully"}
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to delete %s", asset_type)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete {asset_type}.",
            ) from exc


router = APIRouter(prefix="/organizations", tags=["Branding Assets"])
register_asset_endpoints(router, "logo")
register_asset_endpoints(router, "cover")


@router.post("/mock-s3/upload")
async def mock_s3_upload(
    file: UploadFile,
    key: str = Form(...),
):
    """Local-development-only browser upload simulator."""

    if settings.ENVIRONMENT != "development":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        s3 = get_s3_client()
        content = await file.read()
        s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=content)
        return FastApiResponse(status_code=204)
    except Exception as exc:
        logger.error("Local simulated S3 upload failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Local mock upload failed",
        ) from exc
