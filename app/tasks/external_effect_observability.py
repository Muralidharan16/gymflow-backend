"""P4E aggregate observability owned by the isolated maintenance process."""

from __future__ import annotations

import asyncio
import logging

from celery import shared_task
from sqlalchemy import text

from app.core.config import settings
from app.core.database import maintenance_async_session_maker, update_session_context
from app.observability.external_effect_metrics import (
    configure_external_effect_metrics,
    record_operational_snapshots,
)


logger = logging.getLogger(__name__)
_MAINTENANCE_CONTEXT = "lifecycle"

_SEARCH_COLUMNS = (
    "pending_count",
    "processing_count",
    "dead_letter_count",
    "reconciliation_candidate_count",
    "oldest_actionable_age_seconds",
)
_REFUND_COLUMNS = (
    "pending_count",
    "processing_count",
    "retry_pending_count",
    "provider_accepted_count",
    "reconciliation_pending_count",
    "dead_letter_count",
    "oldest_unresolved_age_seconds",
)


async def _prepare_maintenance_session(session) -> None:
    await update_session_context(
        session,
        internal_maintenance=_MAINTENANCE_CONTEXT,
        role="lifecycle_maintenance",
        trace_id="p4e-operational-snapshot",
    )


def _normalize_row(
    row, columns: tuple[str, ...], *, prefix: str
) -> dict[str, int | float]:
    normalized: dict[str, int | float] = {}
    for column in columns:
        value = row[f"{prefix}_{column}"]
        normalized[column] = (
            float(value) if column.endswith("_age_seconds") else int(value)
        )
    return normalized


async def _run_external_effect_operational_snapshot() -> dict[
    str, dict[str, int | float]
]:
    async with maintenance_async_session_maker() as session:
        await _prepare_maintenance_session(session)
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT
                            s.pending_count AS search_pending_count,
                            s.processing_count AS search_processing_count,
                            s.dead_letter_count AS search_dead_letter_count,
                            s.reconciliation_candidate_count
                                AS search_reconciliation_candidate_count,
                            s.oldest_actionable_age_seconds
                                AS search_oldest_actionable_age_seconds,
                            r.pending_count AS refund_pending_count,
                            r.processing_count AS refund_processing_count,
                            r.retry_pending_count AS refund_retry_pending_count,
                            r.provider_accepted_count
                                AS refund_provider_accepted_count,
                            r.reconciliation_pending_count
                                AS refund_reconciliation_pending_count,
                            r.dead_letter_count AS refund_dead_letter_count,
                            r.oldest_unresolved_age_seconds
                                AS refund_oldest_unresolved_age_seconds
                        FROM app_secure.search_operational_snapshot() AS s
                        CROSS JOIN
                            app_secure.refund_execution_operational_snapshot() AS r
                        """
                    )
                )
            ).mappings().one()
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("P4E operational snapshot collection failed")
            raise

    snapshot = {
        "search": _normalize_row(row, _SEARCH_COLUMNS, prefix="search"),
        "refund": _normalize_row(row, _REFUND_COLUMNS, prefix="refund"),
    }
    endpoint = settings.P4E_METRICS_OTLP_ENDPOINT.strip()
    if endpoint:
        configure_external_effect_metrics(
            endpoint=endpoint,
            export_interval_seconds=settings.P4E_METRICS_EXPORT_INTERVAL_SECONDS,
            export_timeout_seconds=settings.P4E_METRICS_EXPORT_TIMEOUT_SECONDS,
            environment=settings.ENVIRONMENT,
        )
        record_operational_snapshots(
            search=snapshot["search"],
            refund=snapshot["refund"],
        )

    logger.info(
        "P4E operational snapshot collected: search_actionable=%s, "
        "search_reconciliation=%s, refund_unresolved=%s",
        sum(
            int(snapshot["search"][key])
            for key in ("pending_count", "processing_count", "dead_letter_count")
        ),
        snapshot["search"]["reconciliation_candidate_count"],
        sum(
            int(snapshot["refund"][key])
            for key in _REFUND_COLUMNS
            if key.endswith("_count")
        ),
    )
    return snapshot


@shared_task(
    name="app.tasks.external_effect_observability.snapshot",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def run_external_effect_operational_snapshot() -> dict[
    str, dict[str, int | float]
]:
    return asyncio.run(_run_external_effect_operational_snapshot())
