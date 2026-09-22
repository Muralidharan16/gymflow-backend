"""P4E/P8 aggregate observability owned by the isolated maintenance process."""

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
from app.observability.runtime_metrics import runtime_metrics
from app.finance_core.observability import sanitized_finance_correlation


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
_PAY18_COLUMNS = (
    "payment_attempt_total",
    "payment_failure_total",
    "payment_unknown_total",
    "webhook_backlog",
    "payment_application_backlog",
    "finance_outbox_backlog",
    "platform_refund_backlog",
    "platform_refund_unknown_total",
    "settlement_mismatch_total",
    "reconciliation_open_total",
    "mandate_failed_total",
    "mandate_expired_total",
    "mandate_revoked_total",
    "dunning_full_grace_total",
    "dunning_limited_write_total",
    "dunning_read_only_total",
    "dunning_billing_only_total",
    "dunning_recovered_total",
    "chargeback_open_total",
    "duplicate_payment_allegation_open_total",
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


def _record_p8_snapshots(snapshot: dict[str, dict[str, int | float]]) -> None:
    metrics = runtime_metrics()
    search = snapshot["search"]
    refund = snapshot["refund"]
    financial = snapshot["financial"]

    search_depth = sum(
        int(search[key])
        for key in ("pending_count", "processing_count", "reconciliation_candidate_count")
    )
    refund_depth = sum(
        int(refund[key])
        for key in (
            "pending_count",
            "processing_count",
            "retry_pending_count",
            "provider_accepted_count",
            "reconciliation_pending_count",
        )
    )

    metrics.queue_snapshot(
        queue="search",
        depth=search_depth,
        oldest_age_seconds=float(search["oldest_actionable_age_seconds"]),
        dead_letters=int(search["dead_letter_count"]),
    )
    metrics.queue_snapshot(
        queue="refund",
        depth=refund_depth,
        oldest_age_seconds=float(refund["oldest_unresolved_age_seconds"]),
        dead_letters=int(refund["dead_letter_count"]),
    )

    # These are aggregate operational evidence from the existing SECURITY
    # DEFINER snapshot. They do not alter refund state or provider authority.
    metrics.finance_snapshot(
        reconciliation_mismatches=int(refund["reconciliation_pending_count"]),
        provider_ack_ambiguity=(
            int(refund["provider_accepted_count"])
            + int(refund["reconciliation_pending_count"])
        ),
        refund_obligations=refund_depth + int(refund["dead_letter_count"]),
    )

    # PAY-18 joins aggregate Platform Billing refund truth with the existing
    # Finance Core refund execution snapshot. No entity identifier is emitted.
    finance_refund_backlog = (
        refund_depth
        + int(refund["dead_letter_count"])
        + int(financial["platform_refund_backlog"])
    )
    finance_refund_unknown = (
        int(refund["reconciliation_pending_count"])
        + int(financial["platform_refund_unknown_total"])
    )
    metrics.financial_observability_snapshot(
        payment_attempt_total=int(financial["payment_attempt_total"]),
        payment_failure_total=int(financial["payment_failure_total"]),
        payment_unknown_total=int(financial["payment_unknown_total"]),
        webhook_backlog=int(financial["webhook_backlog"]),
        payment_application_backlog=int(
            financial["payment_application_backlog"]
        ),
        finance_outbox_backlog=int(financial["finance_outbox_backlog"]),
        refund_backlog=finance_refund_backlog,
        refund_unknown_total=finance_refund_unknown,
        settlement_mismatch_total=int(
            financial["settlement_mismatch_total"]
        ),
        reconciliation_open_total=int(
            financial["reconciliation_open_total"]
        ),
        mandate_failed_total=int(financial["mandate_failed_total"]),
        mandate_expired_total=int(financial["mandate_expired_total"]),
        mandate_revoked_total=int(financial["mandate_revoked_total"]),
        dunning_full_grace_total=int(
            financial["dunning_full_grace_total"]
        ),
        dunning_limited_write_total=int(
            financial["dunning_limited_write_total"]
        ),
        dunning_read_only_total=int(
            financial["dunning_read_only_total"]
        ),
        dunning_billing_only_total=int(
            financial["dunning_billing_only_total"]
        ),
        dunning_recovered_total=int(
            financial["dunning_recovered_total"]
        ),
        chargeback_open_total=int(financial["chargeback_open_total"]),
        duplicate_payment_allegation_open_total=int(
            financial["duplicate_payment_allegation_open_total"]
        ),
    )


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
                                AS refund_oldest_unresolved_age_seconds,
                            p.payment_attempt_total AS financial_payment_attempt_total,
                            p.payment_failure_total AS financial_payment_failure_total,
                            p.payment_unknown_total AS financial_payment_unknown_total,
                            p.webhook_backlog AS financial_webhook_backlog,
                            p.payment_application_backlog
                                AS financial_payment_application_backlog,
                            p.finance_outbox_backlog
                                AS financial_finance_outbox_backlog,
                            p.platform_refund_backlog
                                AS financial_platform_refund_backlog,
                            p.platform_refund_unknown_total
                                AS financial_platform_refund_unknown_total,
                            p.settlement_mismatch_total
                                AS financial_settlement_mismatch_total,
                            p.reconciliation_open_total
                                AS financial_reconciliation_open_total,
                            p.mandate_failed_total
                                AS financial_mandate_failed_total,
                            p.mandate_expired_total
                                AS financial_mandate_expired_total,
                            p.mandate_revoked_total
                                AS financial_mandate_revoked_total,
                            p.dunning_full_grace_total
                                AS financial_dunning_full_grace_total,
                            p.dunning_limited_write_total
                                AS financial_dunning_limited_write_total,
                            p.dunning_read_only_total
                                AS financial_dunning_read_only_total,
                            p.dunning_billing_only_total
                                AS financial_dunning_billing_only_total,
                            p.dunning_recovered_total
                                AS financial_dunning_recovered_total,
                            p.chargeback_open_total
                                AS financial_chargeback_open_total,
                            p.duplicate_payment_allegation_open_total
                                AS financial_duplicate_payment_allegation_open_total
                        FROM app_secure.search_operational_snapshot() AS s
                        CROSS JOIN
                            app_secure.refund_execution_operational_snapshot() AS r
                        CROSS JOIN
                            app_secure.pay18_financial_observability_snapshot() AS p
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
        "financial": _normalize_row(
            row,
            _PAY18_COLUMNS,
            prefix="financial",
        ),
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

    _record_p8_snapshots(snapshot)

    logger.info(
        "Financial operational snapshot collected",
        extra={
            "event": "finance.observability.snapshot",
            "finance_correlation": sanitized_finance_correlation(),
            "search_actionable": sum(
                int(snapshot["search"][key])
                for key in (
                    "pending_count",
                    "processing_count",
                    "dead_letter_count",
                )
            ),
            "search_reconciliation": int(
                snapshot["search"]["reconciliation_candidate_count"]
            ),
            "refund_unresolved": sum(
                int(snapshot["refund"][key])
                for key in _REFUND_COLUMNS
                if key.endswith("_count")
            ),
            "payment_unknown_total": int(
                snapshot["financial"]["payment_unknown_total"]
            ),
            "reconciliation_open_total": int(
                snapshot["financial"]["reconciliation_open_total"]
            ),
        },
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
