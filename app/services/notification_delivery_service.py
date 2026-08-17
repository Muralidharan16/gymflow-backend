"""P4C durable notification execution behind database lease/capability fences."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.core.database import update_session_context, worker_async_session_maker
from app.services.notification_provider import NotificationProviderError, ResendEmailProvider


logger = logging.getLogger("doers.notification_delivery")
_MAINTENANCE_TOKEN = "branch_lifecycle_saga"


async def _install_context(session, event: dict[str, Any], worker_id: uuid.UUID) -> None:
    await update_session_context(
        session,
        org_id=str(event["tenant_id"]),
        trace_id=str(event["correlation_id"]),
        role="branch_lifecycle_worker",
        internal_maintenance=_MAINTENANCE_TOKEN,
        worker_id=str(worker_id),
    )


async def materialize_member_notifications(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        count = await session.scalar(
            text(
                """
                SELECT app_secure.materialize_branch_member_notifications(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid)
                )
                """
            ),
            {"outbox_id": event["outbox_id"], "worker_id": worker_id},
        )
        await session.commit()
    logger.info(
        "Materialized lifecycle member notifications",
        extra={"outbox_id": str(event["outbox_id"]), "command_count": int(count or 0)},
    )
    return "superseded"


async def _claim_delivery(event: dict[str, Any], worker_id: uuid.UUID) -> dict[str, Any]:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        result = await session.execute(
            text(
                """
                SELECT eligible,command_id,tenant_id,branch_id,member_id,channel,destination,
                       member_name,template_key,template_data,idempotency_key,attempt_number,provider_code
                FROM app_secure.claim_notification_delivery_v2(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid)
                )
                """
            ),
            {"outbox_id": event["outbox_id"], "worker_id": worker_id},
        )
        projection = dict(result.mappings().one())
        # Provider I/O is always outside the database transaction.
        await session.commit()
        return projection


async def _ack_delivery(event: dict[str, Any], worker_id: uuid.UUID, evidence) -> None:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        applied = await session.scalar(
            text(
                """
                SELECT app_secure.acknowledge_notification_provider_acceptance(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid),
                    CAST(:provider_reference_id AS text),CAST(:request_sha256 AS text),
                    CAST(:evidence_sha256 AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "provider_reference_id": evidence.provider_reference_id,
                "request_sha256": evidence.request_sha256,
                "evidence_sha256": evidence.provider_evidence_sha256,
            },
        )
        if not applied:
            raise RuntimeError("notification provider acknowledgement was not applied")
        await session.commit()


async def _record_delivery_failure(
    event: dict[str, Any], worker_id: uuid.UUID, error: NotificationProviderError
) -> str:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        result = await session.scalar(
            text(
                """
                SELECT app_secure.record_notification_delivery_failure(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid),CAST(:outcome AS text),
                    CAST(:request_sha256 AS text),CAST(:error_code AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "outcome": error.outcome,
                "request_sha256": error.request_sha256,
                "error_code": error.error_code,
            },
        )
        await session.commit()
    return "dead_lettered" if result == "dead_lettered" else "retry"


async def process_delivery(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    projection = await _claim_delivery(event, worker_id)
    if not bool(projection["eligible"]):
        return "superseded"

    provider = ResendEmailProvider.from_settings()
    try:
        evidence = await provider.send(
            destination=str(projection["destination"]),
            member_name=str(projection["member_name"] or "Member"),
            template_key=str(projection["template_key"]),
            template_data=projection["template_data"] or {},
            idempotency_key=str(projection["idempotency_key"]),
        )
    except NotificationProviderError as exc:
        try:
            return await _record_delivery_failure(event, worker_id, exc)
        except Exception:
            # Do not release the outbox lease after an unknown database commit
            # point. Lease expiry + claim_notification_delivery_v2 will mark the
            # abandoned attempt ambiguous and retry the same logical idempotency key.
            logger.exception(
                "Failed to persist classified notification provider failure; leaving lease for fenced reclaim",
                extra={"outbox_id": str(event["outbox_id"]), "provider_error_code": exc.error_code},
            )
            return "lease_lost"
    except Exception as exc:
        try:
            request_sha256 = provider.request_sha256(
                destination=str(projection["destination"]),
                member_name=str(projection["member_name"] or "Member"),
                template_key=str(projection["template_key"]),
                template_data=projection["template_data"] or {},
                from_email=provider.from_email,
            )
        except Exception:
            request_sha256 = "0" * 64
        classified = NotificationProviderError(
            f"Unexpected notification adapter failure: {type(exc).__name__}",
            outcome="retryable_failure",
            error_code="notification_adapter_internal_error",
            request_sha256=request_sha256,
        )
        try:
            return await _record_delivery_failure(event, worker_id, classified)
        except Exception:
            logger.exception(
                "Failed to persist unexpected notification adapter failure; leaving lease for fenced reclaim",
                extra={"outbox_id": str(event["outbox_id"])},
            )
            return "lease_lost"

    try:
        await _ack_delivery(event, worker_id, evidence)
    except Exception:
        # The provider may already have accepted the effect. Never turn this into
        # a generic queue retry that loses command fencing. The expired in-flight
        # command will be reclaimed as ambiguous using the same idempotency key.
        logger.exception(
            "Provider accepted notification but acknowledgement persistence failed; leaving lease for fenced reclaim",
            extra={
                "outbox_id": str(event["outbox_id"]),
                "provider_reference_id": evidence.provider_reference_id,
            },
        )
        return "lease_lost"

    logger.info(
        "Notification provider accepted command; awaiting terminal evidence",
        extra={
            "outbox_id": str(event["outbox_id"]),
            "provider": evidence.provider_code,
            "provider_reference_id": evidence.provider_reference_id,
        },
    )
    return "provider_accepted"


async def _claim_reconciliation(event: dict[str, Any], worker_id: uuid.UUID) -> dict[str, Any]:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        result = await session.execute(
            text(
                """
                SELECT command_id,tenant_id,provider_reference_id
                FROM app_secure.claim_notification_reconciliation(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid)
                )
                """
            ),
            {"outbox_id": event["outbox_id"], "worker_id": worker_id},
        )
        projection = dict(result.mappings().one())
        await session.commit()
        return projection


async def _complete_reconciliation(event: dict[str, Any], worker_id: uuid.UUID, evidence) -> str:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        result = await session.scalar(
            text(
                """
                SELECT app_secure.complete_notification_reconciliation(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid),
                    CAST(:last_event AS text),CAST(:evidence_sha256 AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "last_event": evidence.last_event,
                "evidence_sha256": evidence.provider_evidence_sha256,
            },
        )
        await session.commit()
    logger.info(
        "Notification reconciliation completed",
        extra={
            "outbox_id": str(event["outbox_id"]),
            "provider_reference_id": evidence.provider_reference_id,
            "last_event": evidence.last_event,
            "result": str(result),
        },
    )
    return "delivered"


async def _record_reconciliation_failure(
    event: dict[str, Any], worker_id: uuid.UUID, error: NotificationProviderError
) -> str:
    async with worker_async_session_maker() as session:
        await _install_context(session, event, worker_id)
        result = await session.scalar(
            text(
                """
                SELECT app_secure.record_notification_reconciliation_failure(
                    CAST(:outbox_id AS uuid),CAST(:worker_id AS uuid),CAST(:error_code AS text),
                    CAST(:permanent AS boolean)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "error_code": error.error_code,
                "permanent": error.outcome == "permanent_rejection",
            },
        )
        await session.commit()
    return str(result)


async def process_reconciliation(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    projection = await _claim_reconciliation(event, worker_id)
    provider = ResendEmailProvider.from_settings()
    try:
        evidence = await provider.reconcile(str(projection["provider_reference_id"]))
    except NotificationProviderError as exc:
        try:
            return await _record_reconciliation_failure(event, worker_id, exc)
        except Exception:
            logger.exception(
                "Failed to persist notification reconciliation failure; leaving lease for reclaim",
                extra={"outbox_id": str(event["outbox_id"]), "provider_error_code": exc.error_code},
            )
            return "lease_lost"
    except Exception as exc:
        classified = NotificationProviderError(
            f"Unexpected notification reconciliation failure: {type(exc).__name__}",
            outcome="retryable_failure",
            error_code="notification_reconciliation_internal_error",
            request_sha256="0" * 64,
        )
        try:
            return await _record_reconciliation_failure(event, worker_id, classified)
        except Exception:
            logger.exception(
                "Failed to persist unexpected reconciliation failure; leaving lease for reclaim",
                extra={"outbox_id": str(event["outbox_id"])},
            )
            return "lease_lost"
    try:
        return await _complete_reconciliation(event, worker_id, evidence)
    except Exception:
        logger.exception(
            "Failed to persist notification reconciliation evidence; leaving lease for reclaim",
            extra={"outbox_id": str(event["outbox_id"])},
        )
        return "lease_lost"


async def process_notification_event(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    event_type = str(event["event_type"])
    if event_type == "branch.member_notification":
        return await materialize_member_notifications(event, worker_id)
    if event_type == "notification.delivery":
        return await process_delivery(event, worker_id)
    if event_type == "notification.reconcile":
        return await process_reconciliation(event, worker_id)
    raise ValueError(f"unsupported P4C notification event: {event_type}")
