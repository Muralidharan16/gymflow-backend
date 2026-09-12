"""P4C provider webhook boundary.

The endpoint is intentionally unauthenticated at the tenant/JWT layer because
Resend is the caller. Authority comes from raw-body Svix verification and the
provider reference is resolved to a tenant-bound notification command inside a
SECURITY DEFINER database capability. No tenant, member, destination or message
body from the webhook becomes authorization authority.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services.resend_webhook import ResendWebhookError, verify_resend_webhook


logger = logging.getLogger("doers.notification_webhooks")
router = APIRouter(prefix="/webhooks/notifications", tags=["Notification Webhooks"])


@router.post("/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_notification_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    raw_body = await request.body()
    try:
        event = verify_resend_webhook(
            raw_body=raw_body,
            event_id=request.headers.get("svix-id", ""),
            timestamp=request.headers.get("svix-timestamp", ""),
            signature_header=request.headers.get("svix-signature", ""),
            secret=settings.RESEND_WEBHOOK_SECRET,
        )
    except ResendWebhookError as exc:
        logger.warning(
            "Rejected Resend notification webhook",
            extra={"reason": str(exc), "request_id": getattr(request.state, "correlation_id", "unknown")},
        )
        raise HTTPException(status_code=400, detail="Invalid webhook") from exc

    result = await db.scalar(
        text(
            """
            SELECT app_secure.apply_resend_notification_event_v2(
                CAST(:event_id AS text),CAST(:provider_reference_id AS text),
                CAST(:event_type AS text),CAST(:event_created_at AS timestamptz),
                CAST(:evidence_sha256 AS text)
            )
            """
        ),
        {
            "event_id": event.event_id,
            "provider_reference_id": event.provider_reference_id,
            "event_type": event.event_type,
            "event_created_at": event.event_created_at,
            "evidence_sha256": event.evidence_sha256,
        },
    )
    logger.info(
        "Processed verified Resend notification webhook",
        extra={"provider_event_id": event.event_id, "event_type": event.event_type, "result": str(result)},
    )
    return {"status": str(result)}
