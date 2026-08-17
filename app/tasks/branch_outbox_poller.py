from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.database import update_session_context, worker_async_session_maker
from app.observability.search_metrics import record_drift_repair
from app.services.branch_lifecycle_service import BranchLifecycleService
from app.services.notification_delivery_service import process_notification_event
from app.services.search_provider import OpenSearchProvider, SearchProviderError


logger = logging.getLogger("doers.branch_lifecycle_outbox")

_BATCH_SIZE = 20
_LEASE_SECONDS = 600
_MAINTENANCE_TOKEN = "branch_lifecycle_saga"
_SEARCH_EVENT_TYPES = {
    "branch.search_deindex",
    "branch.search_index",
}
_NOTIFICATION_EVENT_TYPES = {
    "branch.member_notification",
    "notification.delivery",
    "notification.reconcile",
}
_DEFERRED_EXTERNAL_EVENT_TYPES = {
    "branch.refund_required",
}


async def _claim_events(worker_id: uuid.UUID) -> list[dict[str, Any]]:
    """Lease pending work and reclaim expired processing rows atomically."""

    async with worker_async_session_maker() as session:
        result = await session.execute(
            text(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM public.branch_outbox_events
                    WHERE attempt_count < max_attempts
                      AND process_after <= pg_catalog.clock_timestamp()
                      AND (
                            status = 'pending'
                            OR (
                                status = 'processing'
                                AND leased_until <= pg_catalog.clock_timestamp()
                            )
                      )
                    ORDER BY process_after, created_at, outbox_id
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE public.branch_outbox_events AS outbox_data
                SET status = 'processing',
                    attempt_count = outbox_data.attempt_count + 1,
                    last_attempted_at = pg_catalog.clock_timestamp(),
                    last_error = NULL,
                    leased_by = :worker_id,
                    leased_until = pg_catalog.clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second')
                FROM candidates
                WHERE outbox_data.outbox_id = candidates.outbox_id
                RETURNING
                    outbox_data.outbox_id,
                    outbox_data.tenant_id,
                    outbox_data.branch_id,
                    outbox_data.event_type,
                    outbox_data.payload,
                    outbox_data.attempt_count,
                    outbox_data.max_attempts,
                    outbox_data.correlation_id
                """
            ),
            {
                "worker_id": worker_id,
                "lease_seconds": _LEASE_SECONDS,
                "batch_size": _BATCH_SIZE,
            },
        )
        events = [dict(row) for row in result.mappings().all()]
        await session.commit()
        return events


async def _install_saga_context(
    session,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    await update_session_context(
        session,
        org_id=str(event["tenant_id"]),
        trace_id=str(event["correlation_id"]),
        role="branch_lifecycle_worker",
        internal_maintenance=_MAINTENANCE_TOKEN,
        worker_id=str(worker_id),
    )


async def _mark_delivered(
    session,
    *,
    outbox_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> None:
    result = await session.execute(
        text(
            """
            UPDATE public.branch_outbox_events
            SET status = 'delivered',
                leased_by = NULL,
                leased_until = NULL,
                last_error = NULL
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
            """
        ),
        {"outbox_id": outbox_id, "worker_id": worker_id},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"Lost lifecycle outbox lease before delivery: {outbox_id}"
        )


async def _process_saga_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    payload = event["payload"] or {}
    try:
        branch_id = uuid.UUID(str(payload["branch_id"]))
        org_id = uuid.UUID(str(payload["org_id"]))
        from_status = str(payload["from_status"])
        to_status = str(payload["to_status"])
        actor_id = uuid.UUID(str(payload["actor_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        return await _fail_event(
            event,
            worker_id,
            ValueError(f"Malformed lifecycle saga payload: {exc}"),
            permanent=True,
        )

    if branch_id != event["branch_id"] or org_id != event["tenant_id"]:
        return await _fail_event(
            event,
            worker_id,
            ValueError("Lifecycle saga payload does not match persisted tenant/branch"),
            permanent=True,
        )

    try:
        async with worker_async_session_maker() as session:
            await _install_saga_context(session, event=event, worker_id=worker_id)
            service = BranchLifecycleService(session)
            await service.execute_saga_cascade(
                branch_id=branch_id,
                org_id=org_id,
                from_status=from_status,
                to_status=to_status,
                correlation_id=event["correlation_id"],
                actor_id=actor_id,
                parent_outbox_id=event["outbox_id"],
                worker_id=worker_id,
            )
            await _mark_delivered(
                session,
                outbox_id=event["outbox_id"],
                worker_id=worker_id,
            )
            # Transaction-B DB effects, durable child commands and the parent
            # delivered marker commit together. A crash before commit leaves no
            # partial B state and the lease becomes reclaimable after expiry.
            await session.commit()
        return "delivered"
    except Exception as exc:
        return await _fail_event(event, worker_id, exc, permanent=False)


async def _claim_search_projection(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> dict[str, Any]:
    """Read the authoritative search projection behind the current live lease."""

    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        result = await session.execute(
            text(
                """
                SELECT tenant_id, branch_id, operation, desired_version,
                       document, previous_ack_version
                FROM app_secure.claim_branch_search_projection(
                    CAST(:outbox_id AS uuid), CAST(:worker_id AS uuid)
                )
                """
            ),
            {"outbox_id": event["outbox_id"], "worker_id": worker_id},
        )
        projection = dict(result.mappings().one())
        # Do not hold a database transaction open across provider I/O.
        await session.commit()
        return projection


async def _acknowledge_search_effect(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    *,
    projection: dict[str, Any],
    evidence,
) -> str:
    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        applied = await session.scalar(
            text(
                """
                SELECT app_secure.acknowledge_branch_search_effect(
                    CAST(:outbox_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:desired_version AS bigint),
                    CAST(:operation AS text),
                    CAST(:provider_code AS text),
                    CAST(:provider_index AS text),
                    CAST(:provider_document_id AS text),
                    CAST(:request_sha256 AS text),
                    CAST(:provider_version AS bigint),
                    CAST(:provider_evidence_sha256 AS text),
                    CAST(:document_sha256 AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "desired_version": projection["desired_version"],
                "operation": projection["operation"],
                "provider_code": evidence.provider_code,
                "provider_index": evidence.provider_index,
                "provider_document_id": evidence.provider_document_id,
                "request_sha256": evidence.request_sha256,
                "provider_version": evidence.provider_version,
                "provider_evidence_sha256": evidence.provider_evidence_sha256,
                "document_sha256": evidence.document_sha256,
            },
        )
        await session.commit()
    return "delivered" if bool(applied) else "superseded"


async def _record_search_failure(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    *,
    projection: dict[str, Any],
    error: SearchProviderError,
) -> bool:
    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        still_current = await session.scalar(
            text(
                """
                SELECT app_secure.record_branch_search_failure(
                    CAST(:outbox_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:desired_version AS bigint),
                    CAST(:operation AS text),
                    CAST(:outcome AS text),
                    CAST(:provider_code AS text),
                    CAST(:request_sha256 AS text),
                    CAST(:error_code AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "desired_version": projection["desired_version"],
                "operation": projection["operation"],
                "outcome": error.outcome,
                "provider_code": "opensearch",
                "request_sha256": error.request_sha256,
                "error_code": error.error_code,
            },
        )
        await session.commit()
    return bool(still_current)


async def _repair_search_drift(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    *,
    projection: dict[str, Any],
    error: SearchProviderError,
) -> int | None:
    """Advance the authoritative clock above proved provider drift and requeue."""

    if not error.is_repairable_drift:
        raise ValueError("search drift repair requires complete provider evidence")

    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        next_version = await session.scalar(
            text(
                """
                SELECT app_secure.repair_branch_search_provider_drift(
                    CAST(:outbox_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:desired_version AS bigint),
                    CAST(:operation AS text),
                    CAST(:provider_code AS text),
                    CAST(:provider_index AS text),
                    CAST(:provider_document_id AS text),
                    CAST(:request_sha256 AS text),
                    CAST(:provider_version AS bigint),
                    CAST(:provider_evidence_sha256 AS text),
                    CAST(:document_sha256 AS text),
                    CAST(:error_code AS text)
                )
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "desired_version": projection["desired_version"],
                "operation": projection["operation"],
                "provider_code": "opensearch",
                "provider_index": error.provider_index,
                "provider_document_id": error.provider_document_id,
                "request_sha256": error.request_sha256,
                "provider_version": error.provider_version,
                "provider_evidence_sha256": error.provider_evidence_sha256,
                "document_sha256": error.document_sha256,
                "error_code": error.error_code,
            },
        )
        await session.commit()
    return int(next_version) if next_version is not None else None


async def _process_search_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    """Apply search work only from a live lease and persist provider truth."""

    try:
        projection = await _claim_search_projection(event, worker_id)
    except Exception as exc:
        return await _fail_event(event, worker_id, exc, permanent=False)

    # Event labels and payloads are intentionally ignored for the actual effect.
    # The DB capability re-reads the current branch state and returns the current
    # operation/version/document after proving lease ownership.
    provider = OpenSearchProvider.from_settings()
    branch_id = str(projection["branch_id"])
    operation = str(projection["operation"])
    desired_version = int(projection["desired_version"])
    document = projection["document"]

    try:
        evidence = await provider.apply(
            branch_id=branch_id,
            operation=operation,
            desired_version=desired_version,
            document=document,
        )
    except SearchProviderError as exc:
        if exc.is_repairable_drift:
            try:
                next_version = await _repair_search_drift(
                    event,
                    worker_id,
                    projection=projection,
                    error=exc,
                )
            except Exception as repair_error:
                record_drift_repair(operation=operation, result="repair_failed")
                return await _fail_event(event, worker_id, repair_error, permanent=False)

            result = "requeued" if next_version is not None else "superseded"
            record_drift_repair(operation=operation, result=result)
            logger.warning(
                "Lifecycle search provider drift fenced and requeued",
                extra={
                    "outbox_id": str(event["outbox_id"]),
                    "tenant_id": str(event["tenant_id"]),
                    "branch_id": branch_id,
                    "operation": operation,
                    "desired_version": desired_version,
                    "observed_provider_version": exc.provider_version,
                    "next_version": next_version,
                    "provider_error_code": exc.error_code,
                    "result": result,
                },
            )
            return "superseded"

        try:
            still_current = await _record_search_failure(
                event,
                worker_id,
                projection=projection,
                error=exc,
            )
        except Exception as record_error:
            return await _fail_event(event, worker_id, record_error, permanent=False)
        if not still_current:
            return "superseded"
        return await _fail_event(
            event,
            worker_id,
            exc,
            permanent=exc.outcome == "permanent_rejection",
        )
    except Exception as exc:
        request_sha256 = OpenSearchProvider.request_sha256(
            index=provider.index,
            branch_id=branch_id,
            operation=operation,
            desired_version=desired_version,
            document=document,
        )
        classified = SearchProviderError(
            f"Unexpected OpenSearch adapter failure: {type(exc).__name__}",
            outcome="retryable_failure",
            error_code="adapter_internal_error",
            request_sha256=request_sha256,
        )
        try:
            still_current = await _record_search_failure(
                event,
                worker_id,
                projection=projection,
                error=classified,
            )
        except Exception as record_error:
            return await _fail_event(event, worker_id, record_error, permanent=False)
        if not still_current:
            return "superseded"
        return await _fail_event(event, worker_id, classified, permanent=False)

    try:
        outcome = await _acknowledge_search_effect(
            event,
            worker_id,
            projection=projection,
            evidence=evidence,
        )
    except Exception as exc:
        # The provider may already contain the desired projection. Do not issue a
        # blind compensating write. Lease expiry + strict external versioning +
        # authoritative GET make the retried attempt safe.
        return await _fail_event(event, worker_id, exc, permanent=False)

    logger.info(
        "Lifecycle search effect verified",
        extra={
            "outbox_id": str(event["outbox_id"]),
            "tenant_id": str(event["tenant_id"]),
            "branch_id": branch_id,
            "operation": operation,
            "desired_version": desired_version,
            "provider_version": evidence.provider_version,
            "outcome": outcome,
        },
    )
    return outcome


async def _fail_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    error: Exception,
    *,
    permanent: bool,
) -> str:
    attempts = int(event["attempt_count"])
    max_attempts = int(event["max_attempts"])
    exhausted = permanent or attempts >= max_attempts
    error_text = f"{type(error).__name__}: {error}"[:2000]

    if exhausted and event["event_type"] == "branch.lifecycle_saga" and not permanent:
        payload = event["payload"] or {}
        try:
            async with worker_async_session_maker() as session:
                await _install_saga_context(session, event=event, worker_id=worker_id)
                service = BranchLifecycleService(session)
                await service.compensate_saga_from_dead_letter(
                    branch_id=uuid.UUID(str(payload["branch_id"])),
                    org_id=uuid.UUID(str(payload["org_id"])),
                    from_status=str(payload["from_status"]),
                    to_status=str(payload["to_status"]),
                    correlation_id=event["correlation_id"],
                    actor_id=uuid.UUID(str(payload["actor_id"])),
                    parent_outbox_id=event["outbox_id"],
                    worker_id=worker_id,
                )
                result = await session.execute(
                    text(
                        """
                        UPDATE public.branch_outbox_events
                        SET status = 'dead_lettered',
                            leased_by = NULL,
                            leased_until = NULL,
                            last_error = :last_error
                        WHERE outbox_id = :outbox_id
                          AND status = 'processing'
                          AND leased_by = :worker_id
                        """
                    ),
                    {
                        "outbox_id": event["outbox_id"],
                        "worker_id": worker_id,
                        "last_error": error_text,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError("Lost lifecycle lease during compensation")
                await session.commit()
            logger.error(
                "Lifecycle saga exhausted retries and Transaction A was compensated",
                extra={
                    "outbox_id": str(event["outbox_id"]),
                    "branch_id": str(event["branch_id"]),
                    "correlation_id": str(event["correlation_id"]),
                    "attempt": attempts,
                },
                exc_info=error,
            )
            return "dead_lettered_compensated"
        except Exception as compensation_error:
            # Never leave attempt=max in processing forever. Dead-letter with
            # explicit compensation failure; the frozen branch remains visible
            # to watchdog/operators for manual repair.
            error_text = (
                f"{error_text}; compensation_failed="
                f"{type(compensation_error).__name__}: {compensation_error}"
            )[:2000]
            logger.exception(
                "Lifecycle saga compensation failed after retry exhaustion",
                extra={"outbox_id": str(event["outbox_id"])},
            )

    if exhausted:
        sql = """
            UPDATE public.branch_outbox_events
            SET status = 'dead_lettered',
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
        """
        outcome = "dead_lettered"
        params: dict[str, Any] = {
            "outbox_id": event["outbox_id"],
            "worker_id": worker_id,
            "last_error": error_text,
        }
    else:
        delay_seconds = min(1800, 30 * (2 ** max(attempts - 1, 0)))
        sql = """
            UPDATE public.branch_outbox_events
            SET status = 'pending',
                process_after = pg_catalog.clock_timestamp()
                    + (:delay_seconds * INTERVAL '1 second'),
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
        """
        outcome = "retry"
        params = {
            "outbox_id": event["outbox_id"],
            "worker_id": worker_id,
            "last_error": error_text,
            "delay_seconds": delay_seconds,
        }

    async with worker_async_session_maker() as failure_session:
        result = await failure_session.execute(text(sql), params)
        await failure_session.commit()
    if result.rowcount != 1:
        logger.warning(
            "Unable to release lifecycle event because lease ownership changed",
            extra={
                "outbox_id": str(event["outbox_id"]),
                "worker_id": str(worker_id),
            },
        )
        return "lease_lost"

    log_method = logger.error if exhausted else logger.warning
    log_method(
        "Lifecycle outbox processing failed",
        extra={
            "outbox_id": str(event["outbox_id"]),
            "event_type": event["event_type"],
            "tenant_id": str(event["tenant_id"]),
            "branch_id": str(event["branch_id"]),
            "attempt": attempts,
            "max_attempts": max_attempts,
            "outcome": outcome,
        },
        exc_info=error,
    )
    return outcome


async def _process_deferred_external_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    """Keep P4D refund commands fail-closed until their provider slice exists."""

    return await _fail_event(
        event,
        worker_id,
        RuntimeError(
            f"No production handler is configured for {event['event_type']}"
        ),
        permanent=False,
    )


async def _process_event(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    event_type = event["event_type"]
    if event_type == "branch.lifecycle_saga":
        return await _process_saga_event(event, worker_id)
    if event_type in _SEARCH_EVENT_TYPES:
        return await _process_search_event(event, worker_id)
    if event_type in _NOTIFICATION_EVENT_TYPES:
        try:
            return await process_notification_event(event, worker_id)
        except Exception as exc:
            if event_type in {"notification.delivery", "notification.reconcile"}:
                # Once an external-effect command is claimed, an unexpected
                # failure may have an unknown provider/DB commit point. Preserve
                # the live lease; the fenced crash-recovery path owns reclaim.
                logger.exception(
                    "Unexpected P4C notification processing failure; preserving lease for fenced reclaim",
                    extra={"outbox_id": str(event["outbox_id"]), "event_type": event_type},
                )
                return "lease_lost"
            return await _fail_event(event, worker_id, exc, permanent=False)
    if event_type in _DEFERRED_EXTERNAL_EVENT_TYPES:
        return await _process_deferred_external_event(event, worker_id)
    return await _fail_event(
        event,
        worker_id,
        ValueError(f"Unsupported lifecycle outbox event type: {event_type}"),
        permanent=True,
    )


async def _poll_outbox() -> dict[str, int]:
    worker_id = uuid.uuid4()
    events = await _claim_events(worker_id)
    summary = {
        "claimed": len(events),
        "delivered": 0,
        "provider_accepted": 0,
        "superseded": 0,
        "retry": 0,
        "dead_lettered": 0,
        "dead_lettered_compensated": 0,
        "lease_lost": 0,
    }

    for event in events:
        outcome = await _process_event(event, worker_id)
        if outcome in summary:
            summary[outcome] += 1

    if events:
        logger.info(
            "Lifecycle outbox poll completed",
            extra={"worker_id": str(worker_id), **summary},
        )
    return summary


@celery_app.task(name="app.tasks.branch_outbox_poller.run")
def run() -> dict[str, int]:
    return asyncio.run(_poll_outbox())
