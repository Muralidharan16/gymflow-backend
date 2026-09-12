import logging
import uuid

import sqlalchemy as sa
from celery import shared_task

from app.core.config import settings
from app.core.database import WorkerSyncSessionLocal
from app.tasks.base_image import run_image_pipeline
from app.utils.s3 import get_s3_client


logger = logging.getLogger("doers")


@shared_task(name="app.tasks.logos.process_organization_asset")
def process_organization_asset(job_id: str) -> bool:
    """Process a DB-authorized organization asset job by opaque job UUID only."""

    return run_image_pipeline(job_id)


def _claim_cleanup(cleanup_id: str, lease_token: str) -> str | None:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        key = db.scalar(
            sa.text(
                """
                SELECT app_secure.claim_organization_asset_cleanup(
                    CAST(:cleanup_id AS uuid), CAST(:lease_token AS uuid), 120
                )
                """
            ),
            {"cleanup_id": cleanup_id, "lease_token": lease_token},
        )
        db.commit()
        return str(key) if key is not None else None


def _finish_cleanup(cleanup_id: str, lease_token: str) -> bool:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        completed = db.scalar(
            sa.text(
                """
                SELECT app_secure.complete_organization_asset_cleanup(
                    CAST(:cleanup_id AS uuid), CAST(:lease_token AS uuid)
                )
                """
            ),
            {"cleanup_id": cleanup_id, "lease_token": lease_token},
        )
        db.commit()
        return bool(completed)


def _fail_cleanup(cleanup_id: str, lease_token: str) -> str | None:
    if WorkerSyncSessionLocal is None:
        raise RuntimeError("Worker sync database session is unavailable")
    with WorkerSyncSessionLocal() as db:
        status = db.scalar(
            sa.text(
                """
                SELECT app_secure.fail_organization_asset_cleanup(
                    CAST(:cleanup_id AS uuid), CAST(:lease_token AS uuid),
                    's3_delete_error'
                )
                """
            ),
            {"cleanup_id": cleanup_id, "lease_token": lease_token},
        )
        db.commit()
        return str(status) if status is not None else None


@shared_task(name="app.tasks.logos.cleanup_organization_asset")
def cleanup_organization_asset(cleanup_id: str) -> bool:
    """Delete exactly one S3 key obtained from a leased DB cleanup intent."""

    lease_token = str(uuid.uuid4())
    key = _claim_cleanup(cleanup_id, lease_token)
    if key is None:
        return True

    try:
        get_s3_client().delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
    except Exception:
        status = _fail_cleanup(cleanup_id, lease_token)
        if status == "failed":
            logger.error(
                "Asset cleanup %s exhausted its bounded retry budget for key %s",
                cleanup_id,
                key,
                exc_info=True,
            )
        else:
            logger.warning(
                "Asset cleanup %s will be redispatched after S3 failure",
                cleanup_id,
                exc_info=True,
            )
        return False

    if not _finish_cleanup(cleanup_id, lease_token):
        # S3 DELETE is idempotent. If the lease was lost after deletion, the
        # bounded dispatcher can safely execute the same persisted key again.
        logger.warning("Asset cleanup %s lost its completion fence", cleanup_id)
        return False
    return True


@shared_task(name="app.tasks.logos.process_org_logo")
def process_org_logo(*_args, **_kwargs):
    """Reject stale pre-P3E queue messages that carried trusted authority fields."""

    raise RuntimeError(
        "Legacy logo queue payloads are disabled; enqueue a durable asset job instead"
    )


@shared_task(name="app.tasks.logos.delete_old_s3_assets")
def delete_old_s3_assets(*_args, **_kwargs):
    """Reject arbitrary-key deletion messages from the legacy queue contract."""

    raise RuntimeError(
        "Arbitrary queued S3-key deletion is disabled by the P3E asset boundary"
    )


@shared_task(name="app.tasks.logos.cleanup_orphaned_logos")
def cleanup_orphaned_logos(*_args, **_kwargs):
    """Keep the legacy cross-tenant object sweep fail-closed."""

    raise RuntimeError(
        "Legacy global orphan-logo cleanup is disabled pending bounded cleanup authority"
    )
