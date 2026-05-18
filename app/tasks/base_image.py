import io
import uuid
import struct
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Literal, Optional, Dict
from PIL import Image, ImageOps
import clamd
from app.core.config import settings
from app.utils.s3 import get_s3_client
from app.models.organization import Organization, AssetStatus, OrganizationAssetAudit
from app.core.database import SessionLocal

logger = logging.getLogger("doers")
Image.MAX_IMAGE_PIXELS = 16_000_000

@dataclass
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
    except clamd.ConnectionError:
        raise ValueError("Antivirus service unavailable")

def get_jpeg_dimensions(file_bytes: bytes) -> tuple[int, int]:
    i = 2
    while i < len(file_bytes):
        if file_bytes[i] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        marker = file_bytes[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = struct.unpack('>H', file_bytes[i+5:i+7])[0]
            w = struct.unpack('>H', file_bytes[i+7:i+9])[0]
            return w, h
        length = struct.unpack('>H', file_bytes[i+2:i+4])[0]
        i += 2 + length
    raise ValueError("SOF marker not found")

def get_webp_dimensions(file_bytes: bytes) -> tuple[int, int]:
    if file_bytes[8:12] != b'WEBP':
        raise ValueError("Not a valid WebP")
    chunk_type = file_bytes[12:16]
    if chunk_type == b'VP8 ':
        w = struct.unpack('<H', file_bytes[26:28])[0] & 0x3FFF
        h = struct.unpack('<H', file_bytes[28:30])[0] & 0x3FFF
        return w, h
    elif chunk_type == b'VP8L':
        bits = struct.unpack('<I', file_bytes[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    raise ValueError("Unsupported WebP format")

def check_dimensions_safe(file_bytes: bytes, min_w: int, min_h: int) -> tuple[int, int]:
    # Detect magic bytes and parse dimensions
    if file_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        w, h = struct.unpack('>II', file_bytes[16:24])
    elif file_bytes[:3] == b'\xff\xd8\xff':
        w, h = get_jpeg_dimensions(file_bytes)
    elif file_bytes[:4] == b'RIFF' and file_bytes[8:12] == b'WEBP':
        w, h = get_webp_dimensions(file_bytes)
    else:
        raise ValueError("Unsupported or invalid magic bytes")

    if w < min_w or h < min_h:
        raise ValueError(f"Image too small: {w}x{h} (required: {min_w}x{min_h})")

    return w, h

def resize_and_pad(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    img.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    new_img = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
    new_img.paste(img, ((target_w - img.width) // 2, (target_h - img.height) // 2))
    return new_img

def resize_cover_focal(img: Image.Image, target_width: int, focal_y: float) -> Image.Image:
    target_ratio = 16 / 9
    target_height = int(target_width / target_ratio)
    scale = target_width / img.width
    scaled = img.resize((target_width, int(img.height * scale)), Image.Resampling.LANCZOS)
    crop_top = int((scaled.height - target_height) * focal_y)
    crop_top = max(0, min(crop_top, scaled.height - target_height))
    return scaled.crop((0, crop_top, target_width, crop_top + target_height))

def run_image_pipeline(
    org_id: str,
    upload_id: str,
    user_id: str,
    request_ip: Optional[str],
    config: AssetPipelineConfig
) -> bool:
    s3 = get_s3_client()
    quarantine_key = f"quarantine/{upload_id}"

    def mark_db_failed(reason: str, extra: Optional[dict] = None):
        detail = {"reason": reason}
        if extra:
            detail.update(extra)
        if request_ip:
            detail["ip_address"] = request_ip

        with SessionLocal() as db:
            org = db.query(Organization).filter(Organization.id == org_id).first()
            if org:
                if config.asset_type == "logo":
                    org.logo_status = AssetStatus.failed
                else:
                    org.cover_status = AssetStatus.failed

                audit = OrganizationAssetAudit(
                    org_id=org_id,
                    changed_by=user_id,
                    asset_type=config.asset_type,
                    action="failed",
                    action_detail=detail,
                    ip_address=request_ip
                )
                db.add(audit)
                db.commit()

    # 0. Fetch file from S3 quarantine
    try:
        response = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
        file_bytes = response['Body'].read()
    except Exception as e:
        logger.error(f"Failed to fetch quarantine object {quarantine_key}: {str(e)}")
        mark_db_failed("s3_fetch_error", {"detail": str(e)})
        return False

    # 1. Antivirus check
    try:
        if not scan_with_clamav(file_bytes):
            s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
            mark_db_failed("malware_detected")
            return False
    except ValueError as e:
        mark_db_failed("antivirus_unavailable")
        return False

    # 2. Pre-flight safe dimension and type check
    try:
        w, h = check_dimensions_safe(file_bytes, config.min_width, config.min_height)
    except ValueError as e:
        s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
        mark_db_failed("validation_failed", {"detail": str(e)})
        return False

    # 3. Process image with Pillow
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        img_clean = img.copy()
        img_clean.info = {}

        asset_uuid = uuid.uuid4().hex
        keys = {}

        for size_name, target_width in config.sizes.items():
            if config.resize_strategy == "pad":
                processed_img = resize_and_pad(img_clean, target_width, target_width)
            else:
                processed_img = resize_cover_focal(img_clean, target_width, config.focal_y)

            s3_key = f"{config.asset_type}s/{org_id}/{asset_uuid}_{size_name}.webp"
            buffer = io.BytesIO()
            # Set dynamic qualities per target size spec:
            quality = 80 if size_name in ("thumb", "mobile") else (85 if size_name in ("medium", "tablet") else 90)
            processed_img.save(buffer, format="WEBP", quality=quality)
            s3.put_object(
                Bucket=settings.S3_BUCKET_NAME,
                Key=s3_key,
                Body=buffer.getvalue(),
                ContentType='image/webp'
            )
            keys[size_name] = s3_key

        # Save clean original
        original_key = f"originals/{org_id}/{asset_uuid}_original"
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=original_key,
            Body=file_bytes,
            ContentType='image/jpeg' if file_bytes[:3] == b'\xff\xd8\xff' else ('image/webp' if file_bytes[8:12] == b'WEBP' else 'image/png')
        )
        keys["original"] = original_key

    except Exception as e:
        logger.error(f"Image scaling failed: {str(e)}")
        s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
        mark_db_failed("processing_failed")
        return False

    # 4. Atomic Database Update
    old_keys = []
    with SessionLocal() as db:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if org:
            if config.asset_type == "logo":
                if org.logo_key: old_keys.append(org.logo_key)
                if org.logo_thumb_key: old_keys.append(org.logo_thumb_key)
                if org.logo_medium_key: old_keys.append(org.logo_medium_key)
                if org.logo_full_key: old_keys.append(org.logo_full_key)

                org.logo_key = keys["original"]
                org.logo_thumb_key = keys["thumb"]
                org.logo_medium_key = keys["medium"]
                org.logo_full_key = keys["full"]
                org.logo_status = AssetStatus.ready
                org.logo_meta = {
                    "width": w,
                    "height": h,
                    "size_bytes": len(file_bytes),
                    "mime_type": "image/webp"
                }
                org.logo_updated_at = datetime.utcnow()
                org.logo_updated_by = user_id
            else:
                if org.cover_key: old_keys.append(org.cover_key)
                if org.cover_mobile_key: old_keys.append(org.cover_mobile_key)
                if org.cover_tablet_key: old_keys.append(org.cover_tablet_key)
                if org.cover_desktop_key: old_keys.append(org.cover_desktop_key)

                org.cover_key = keys["original"]
                org.cover_mobile_key = keys["mobile"]
                org.cover_tablet_key = keys["tablet"]
                org.cover_desktop_key = keys["desktop"]
                org.cover_status = AssetStatus.ready
                org.cover_meta = {
                    "width": w,
                    "height": h,
                    "size_bytes": len(file_bytes),
                    "mime_type": "image/webp",
                    "focal_point_y": config.focal_y
                }
                org.cover_updated_at = datetime.utcnow()
                org.cover_updated_by = user_id

            audit = OrganizationAssetAudit(
                org_id=org_id,
                changed_by=user_id,
                asset_type=config.asset_type,
                action="uploaded",
                old_s3_key=old_keys[0] if old_keys else None,
                new_s3_key=keys["original"],
                ip_address=request_ip
            )
            db.add(audit)
            db.commit()

    # 5. Cleanup quarantine and trigger deletion of replaced assets
    s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=quarantine_key)
    if old_keys:
        from app.tasks.logos import delete_old_s3_assets
        delete_old_s3_assets.delay(old_keys)

    return True
