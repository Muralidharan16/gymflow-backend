import io
import logging
import struct
import uuid
from dataclasses import dataclass
from typing import Dict, Literal, Optional

import clamd
import sqlalchemy as sa
from PIL import Image, ImageOps

from app.core.config import settings
from app.core.database import WorkerSyncSessionLocal
from app.utils.s3 import get_s3_client


logger = logging.getLogger("doers")
Image.MAX_IMAGE_PIXELS = 16_000_000

_ASSET_LEASE_SECONDS = 600


@dataclass(frozen=True)
class AssetPipelineConfig:
    asset_type: Literal["logo", "cover"]
    sizes: Dict[str, int]
    resize_strategy: Literal["pad", "focal_crop"]
    allow_svg: bool
    max_size_bytes: int
    min_width: int
    min_height: int
    focal_y: float = 0.5


def scan_with_clamav(file_bytes: bytes) -> bool:
    if settings.ENVIRONMENT == "development":
        logger.warning("ClamAV antivirus scan bypassed in local development environment.")
        return True
    try:
        cd = clamd.ClamdNetworkSocket(host="clamav", port=3310, timeout=30)
        result = cd.instream(io.BytesIO(file_bytes))
        return result["stream"][0] == "OK"
    except clamd.ConnectionError as exc:
        raise ValueError("Antivirus service unavailable") from exc


def get_jpeg_dimensions(file_bytes: bytes) -> tuple[int, int]:
    i = 2
    while i < len(file_bytes):
        if i + 9 > len(file_bytes) or file_bytes[i] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        marker = file_bytes[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = struct.unpack(">H", file_bytes[i + 5 : i + 7])[0]
            w = struct.unpack(">H", file_bytes[i + 7 : i + 9])[0]
            return w, h
        length = struct.unpack(">H", file_bytes[i + 2 : i + 4])[0]
        if length < 2:
            raise ValueError("Invalid JPEG segment length")
        i += 2 + length
    raise ValueError("SOF marker not found")


def get_webp_dimensions(file_bytes: bytes) -> tuple[int, int]:
    if len(file_bytes) < 30 or file_bytes[8:12] != b"WEBP":
        raise ValueError("Not a valid WebP")
    chunk_type = file_bytes[12:16]
    if chunk_type == b"VP8 ":
        w = struct.unpack("<H", file_bytes[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", file_bytes[28:30])[0] & 0x3FFF
        return w, h
    if chunk_type == b"VP8L":
        bits = struct.unpack("<I", file_bytes[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    raise ValueError("Unsupported WebP format")


def check_dimensions_safe(file_bytes: bytes, min_w: int, min_h: int) -> tuple[int, int]:
    if len(file_bytes) < 30:
        raise ValueError("Image payload is too small")
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", file_bytes[16:24])
    elif file_bytes[:3] == b"\xff\xd8\xff":
        w, h = get_jpeg_dimensions(file_bytes)
    elif file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        w, h = get_webp_dimensions(file_bytes)
    else:
        raise ValueError("Unsupported or invalid magic bytes")
    if w < min_w or h < min_h:
        raise ValueError(f"Image too small: {w}x{h} (required: {min_w}x{min_h})")
    if w * h > Image.MAX_IMAGE_PIXELS:
        raise ValueError("Image pixel count exceeds the processing limit")
    return w, h


def resize_and_pad(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    # thumbnail() mutates its receiver. Work from a copy so generating the thumb
    # cannot silently downsample the source used by medium/full derivatives.
    working = img.copy()
    working.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    new_img = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
    new_img.paste(
        working,
        ((target_w - working.width) // 2, (target_h - working.height) // 2),
    )
    return new_img


def resize_cover_focal(img: Image.Image, target_width: int, focal_y: float) -> Image.Image:
    target_height = int(round(target_width / (16 / 9)))
    scale = max(target_width / img.width, target_height / img.height)
    scaled_width = max(target_width, int(round(img.width * scale)))
    scaled_height = max(target_height, int(round(img.height * scale)))
    scaled = img.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

    crop_left = max(0, (scaled.width - target_width) // 2)
    available_vertical = max(0, scaled.height - target_height)
    crop_top = int(round(available_vertical * focal_y))
    crop_top = max(0, min(crop_top, available_vertical))
    return scaled.crop(
        (
            crop_left,
            crop_top,
            crop_left + target_width,
            crop_top + target_height,
        )
    )


def _pipeline_config(asset_type: str, focal_y: Optional[float]) -> AssetPipelineConfig:
    if asset_type == "logo":
        return AssetPipelineConfig(
            asset_type="logo",
            sizes={"thumb": 128, "medium": 256, "full": 512},
            resize_strategy="pad",
            allow_svg=False,
            max_size_bytes=5_242_880,
            min_width=200,
            min_height=200,
        )
    if asset_type == "cover":
        return AssetPipelineConfig(
            asset_type="cover",
            sizes={"mobile": 640, "tablet": 1024, "desktop": 1920},
            resize_strategy="focal_crop",
            allow_svg=False,
            max_size_bytes=10_485_760,
            min_width=1200,
            min_height=400,
            focal_y=float(focal_y if focal_y is not None else 0.5),
        )
    raise ValueError(f"Unsupported organization asset type: {asset_type!r}")


def _content_type(file_bytes: bytes) -> str:
    if file_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported image content type")


def _claim_job(job_id: str, lease_token: str) -> Optional[dict]:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        row = db.execute(
            sa.text(
                """
                SELECT organization_id, asset_type, upload_id, focal_y,
                       request_ip, requested_by_owner_id, attempt_count
                FROM app_secure.claim_organization_asset_job(
                    CAST(:job_id AS uuid), CAST(:lease_token AS uuid), :lease_seconds
                )
                """
            ),
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "lease_seconds": _ASSET_LEASE_SECONDS,
            },
        ).mappings().one_or_none()
        db.commit()
        return dict(row) if row is not None else None


def _record_failure(job_id: str, lease_token: str, failure_code: str) -> Optional[str]:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        status = db.scalar(
            sa.text(
                """
                SELECT app_secure.fail_organization_asset_job(
                    CAST(:job_id AS uuid), CAST(:lease_token AS uuid), :failure_code
                )
                """
            ),
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "failure_code": failure_code,
            },
        )
        db.commit()
        return str(status) if status is not None else None


def _finalize_job(
    job_id: str,
    lease_token: str,
    *,
    width: int,
    height: int,
    size_bytes: int,
    content_type: str,
) -> tuple[bool, list[str]]:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        row = db.execute(
            sa.text(
                """
                SELECT applied, old_keys
                FROM app_secure.finalize_organization_asset_job(
                    CAST(:job_id AS uuid), CAST(:lease_token AS uuid),
                    :width, :height, :size_bytes, :content_type
                )
                """
            ),
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "width": width,
                "height": height,
                "size_bytes": size_bytes,
                "content_type": content_type,
            },
        ).mappings().one()
        db.commit()
        return bool(row["applied"]), [str(key) for key in (row["old_keys"] or [])]


def _asset_keys(asset_type: str, org_id: str, upload_id: str) -> dict[str, str]:
    result = {"original": f"originals/{org_id}/{upload_id}_original"}
    if asset_type == "logo":
        result.update(
            {
                "thumb": f"logos/{org_id}/{upload_id}_thumb.webp",
                "medium": f"logos/{org_id}/{upload_id}_medium.webp",
                "full": f"logos/{org_id}/{upload_id}_full.webp",
            }
        )
    elif asset_type == "cover":
        result.update(
            {
                "mobile": f"covers/{org_id}/{upload_id}_mobile.webp",
                "tablet": f"covers/{org_id}/{upload_id}_tablet.webp",
                "desktop": f"covers/{org_id}/{upload_id}_desktop.webp",
            }
        )
    else:
        raise ValueError(f"Unsupported organization asset type: {asset_type!r}")
    return result


def _delete_keys_best_effort(s3, keys: list[str]) -> None:
    for key in dict.fromkeys(key for key in keys if key):
        try:
            s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        except Exception:
            logger.warning("Best-effort S3 cleanup failed for %s", key, exc_info=True)


def run_image_pipeline(job_id: str) -> bool:
    """Process one durable asset job using DB-backed authority and fencing.

    Celery supplies only the opaque job UUID. PostgreSQL returns the authoritative
    org/upload/type after a successful lease claim and derives the final keys again
    during finalization. Retries therefore overwrite the same deterministic object
    keys and stale/superseded workers cannot publish their result into DB state.
    """

    lease_token = str(uuid.uuid4())
    job = _claim_job(job_id, lease_token)
    if job is None:
        logger.info("Asset job %s is not claimable; treating delivery as a no-op", job_id)
        return True

    org_id = str(job["organization_id"])
    upload_id = str(job["upload_id"])
    config = _pipeline_config(str(job["asset_type"]), job.get("focal_y"))
    s3 = get_s3_client()
    quarantine_key = f"quarantine/{org_id}/{upload_id}"
    output_keys = _asset_keys(config.asset_type, org_id, upload_id)

    try:
        response = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
        file_bytes = response["Body"].read(config.max_size_bytes + 1)
    except Exception:
        logger.warning("Asset job %s could not fetch quarantine object", job_id, exc_info=True)
        _record_failure(job_id, lease_token, "s3_fetch_error")
        return False

    if len(file_bytes) < 1024 or len(file_bytes) > config.max_size_bytes:
        _delete_keys_best_effort(s3, [quarantine_key])
        _record_failure(job_id, lease_token, "validation_failed")
        return False

    try:
        if not scan_with_clamav(file_bytes):
            _delete_keys_best_effort(s3, [quarantine_key])
            _record_failure(job_id, lease_token, "malware_detected")
            return False
    except ValueError:
        logger.warning("Asset antivirus service unavailable for job %s", job_id)
        _record_failure(job_id, lease_token, "antivirus_unavailable")
        return False

    try:
        width, height = check_dimensions_safe(
            file_bytes,
            config.min_width,
            config.min_height,
        )
        source_content_type = _content_type(file_bytes)
    except ValueError:
        _delete_keys_best_effort(s3, [quarantine_key])
        _record_failure(job_id, lease_token, "validation_failed")
        return False

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGBA")
            source.info = {}

            for size_name, target_width in config.sizes.items():
                processed = (
                    resize_and_pad(source, target_width, target_width)
                    if config.resize_strategy == "pad"
                    else resize_cover_focal(source, target_width, config.focal_y)
                )
                buffer = io.BytesIO()
                quality = (
                    80
                    if size_name in ("thumb", "mobile")
                    else 85
                    if size_name in ("medium", "tablet")
                    else 90
                )
                processed.save(buffer, format="WEBP", quality=quality)
                s3.put_object(
                    Bucket=settings.S3_BUCKET_NAME,
                    Key=output_keys[size_name],
                    Body=buffer.getvalue(),
                    ContentType="image/webp",
                )

        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=output_keys["original"],
            Body=file_bytes,
            ContentType=source_content_type,
        )
    except Exception:
        logger.exception("Asset image processing failed for job %s", job_id)
        _delete_keys_best_effort(s3, list(output_keys.values()))
        _record_failure(job_id, lease_token, "processing_failed")
        return False

    try:
        applied, old_keys = _finalize_job(
            job_id,
            lease_token,
            width=width,
            height=height,
            size_bytes=len(file_bytes),
            content_type=source_content_type,
        )
    except Exception:
        # DB uncertainty is different from an image failure. Leave deterministic
        # outputs in place and let the lease expire; a retried worker can safely
        # overwrite them and PostgreSQL remains the publication authority.
        logger.exception("Asset DB finalization failed for job %s", job_id)
        raise

    if not applied:
        logger.info("Asset job %s lost its publication fence; cleaning staged outputs", job_id)
        _delete_keys_best_effort(
            s3,
            [quarantine_key, *output_keys.values()],
        )
        return True

    _delete_keys_best_effort(s3, [quarantine_key, *old_keys])
    return True
