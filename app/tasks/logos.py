import logging
from celery import current_app
from app.tasks.base_image import run_image_pipeline, AssetPipelineConfig
from app.utils.s3 import get_s3_client
from app.core.config import settings
from app.models.organization import Organization, AssetStatus
from app.core.database import WorkerSyncSessionLocal

logger = logging.getLogger("doers")

@current_app.task(bind=True, max_retries=3)
def process_org_logo(self, org_id: str, upload_id: str, user_id: str, request_ip: str = None):
    config = AssetPipelineConfig(asset_type="logo", sizes={"thumb": 64, "medium": 256, "full": 1024}, resize_strategy="pad", allow_svg=False, max_size_bytes=5_242_880, min_width=200, min_height=200)
    run_image_pipeline(org_id=org_id, upload_id=upload_id, user_id=user_id, request_ip=request_ip, config=config)

@current_app.task
def delete_old_s3_assets(keys: list[str]):
    s3 = get_s3_client()
    for key in keys:
        try:
            s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        except Exception as e:
            logger.error(f"Failed to delete old asset {key}: {str(e)}")

@current_app.task
def cleanup_orphaned_logos():
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    s3 = get_s3_client()
    import datetime
    threshold_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    paginator = s3.get_paginator('list_objects_v2')
    orphans_to_delete = []
    valid_keys = set()
    offset = 0
    batch_size = 500
    with WorkerSyncSessionLocal() as db:
        while True:
            orgs = db.query(Organization).with_entities(Organization.logo_key, Organization.logo_thumb_key, Organization.logo_medium_key, Organization.logo_full_key, Organization.cover_key, Organization.cover_mobile_key, Organization.cover_tablet_key, Organization.cover_desktop_key).offset(offset).limit(batch_size).all()
            if not orgs:
                break
            for org in orgs:
                valid_keys.update(k for k in org if k is not None)
            offset += batch_size
    for prefix in ('logos/', 'covers/', 'originals/'):
        for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME, Prefix=prefix):
            if 'Contents' not in page:
                continue
            for obj in page['Contents']:
                if obj['LastModified'] < threshold_date and obj['Key'] not in valid_keys:
                    orphans_to_delete.append(obj['Key'])
    for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME, Prefix='quarantine/'):
        if 'Contents' not in page:
            continue
        for obj in page['Contents']:
            if obj['LastModified'] < threshold_date:
                orphans_to_delete.append(obj['Key'])
    for key in orphans_to_delete:
        try:
            s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        except Exception:
            pass
