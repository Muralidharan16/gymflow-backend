from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.accounting_reconciliation import (
    FinancialObservation,
    ReconciliationDecision,
)
from app.platform_billing.models.accounting_reconciliation import (
    PlatformAccountingClosureRun,
    PlatformAccountingEvidence,
    PlatformAccountingIncident,
    PlatformAccountingReconciliationItem,
)


class PlatformAccountingReconciliationRepository:
    """PAY-14 persistence boundary.

    This repository intentionally has no dependency on payment/refund/dispute
    repositories and exposes no method that mutates financial truth. It stores
    only closure metadata, evidence, reconciliation decisions, and incidents.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def set_tenant_context(self, organization_id: uuid.UUID | None) -> None:
        if organization_id is None:
            return
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )

    async def create_closure(
        self,
        *,
        closure_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        provider_code: str,
        environment: str,
        closure_key: str,
        period_start: datetime,
        period_end: datetime,
        expected_object_count: int,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)
        inserted = (
            await self._session.execute(
                insert(PlatformAccountingClosureRun)
                .values(
                    id=closure_id,
                    organization_id=organization_id,
                    provider_code=provider_code,
                    environment=environment,
                    closure_key=closure_key,
                    period_start=period_start,
                    period_end=period_end,
                    status="collecting",
                    expected_object_count=expected_object_count,
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_accounting_closure_runs_key"
                )
                .returning(PlatformAccountingClosureRun.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted
        return (
            await self._session.execute(
                select(PlatformAccountingClosureRun.id).where(
                    PlatformAccountingClosureRun.provider_code == provider_code,
                    PlatformAccountingClosureRun.environment == environment,
                    PlatformAccountingClosureRun.closure_key == closure_key,
                )
            )
        ).scalar_one()

    async def append_evidence(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        provider_code: str,
        environment: str,
        observation: FinancialObservation,
        evidence_kind: str,
        local_object_id: uuid.UUID | None = None,
        safe_metadata_json: dict[str, object] | None = None,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)
        inserted = (
            await self._session.execute(
                insert(PlatformAccountingEvidence)
                .values(
                    closure_run_id=closure_run_id,
                    organization_id=organization_id,
                    side=observation.side,
                    object_type=observation.object_type,
                    local_object_id=local_object_id,
                    object_ref=observation.object_ref,
                    provider_code=provider_code,
                    environment=environment,
                    amount_minor=observation.amount_minor,
                    fee_minor=observation.fee_minor,
                    currency_code=observation.currency_code,
                    object_status=observation.status,
                    evidence_kind=evidence_kind,
                    evidence_sha256=observation.evidence_sha256,
                    evidence_ref=observation.evidence_ref,
                    authoritative=observation.authoritative,
                    observed_at=observation.observed_at,
                    safe_metadata_json=safe_metadata_json or {},
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_accounting_evidence_fact"
                )
                .returning(PlatformAccountingEvidence.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted

        return (
            await self._session.execute(
                select(PlatformAccountingEvidence.id).where(
                    PlatformAccountingEvidence.closure_run_id == closure_run_id,
                    PlatformAccountingEvidence.side == observation.side,
                    PlatformAccountingEvidence.object_type == observation.object_type,
                    PlatformAccountingEvidence.object_ref == observation.object_ref,
                    PlatformAccountingEvidence.evidence_sha256
                    == observation.evidence_sha256,
                )
            )
        ).scalar_one()

    async def record_decision(
        self,
        *,
        item_id: uuid.UUID,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        object_type: str,
        reconciliation_key: str,
        decision: ReconciliationDecision,
        first_detected_at: datetime,
        last_checked_at: datetime,
        local_evidence_id: uuid.UUID | None,
        provider_evidence_id: uuid.UUID | None,
        settlement_evidence_id: uuid.UUID | None,
        local_object_id: uuid.UUID | None = None,
        resolution_evidence_sha256: str | None = None,
        resolution_evidence_ref: str | None = None,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)
        resolution_status = _resolution_status_for(decision.safe_outcome)
        resolved_at = (
            last_checked_at if resolution_status == "resolved" else None
        )
        inserted = (
            await self._session.execute(
                insert(PlatformAccountingReconciliationItem)
                .values(
                    id=item_id,
                    closure_run_id=closure_run_id,
                    organization_id=organization_id,
                    object_type=object_type,
                    reconciliation_key=reconciliation_key,
                    local_object_id=local_object_id,
                    local_evidence_id=local_evidence_id,
                    provider_evidence_id=provider_evidence_id,
                    settlement_evidence_id=settlement_evidence_id,
                    mismatch_category=decision.mismatch_category,
                    safe_outcome=decision.safe_outcome,
                    resolution_status=resolution_status,
                    authoritative_evidence_complete=
                        decision.authoritative_evidence_complete,
                    automatic_financial_mutation_allowed=False,
                    reason_code=decision.reason_code,
                    resolution_evidence_sha256=resolution_evidence_sha256,
                    resolution_evidence_ref=resolution_evidence_ref,
                    first_detected_at=first_detected_at,
                    last_checked_at=last_checked_at,
                    resolved_at=resolved_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_accounting_reconciliation_item_key"
                )
                .returning(PlatformAccountingReconciliationItem.id)
            )
        ).scalar_one_or_none()

        if inserted is None:
            inserted = (
                await self._session.execute(
                    select(PlatformAccountingReconciliationItem.id).where(
                        PlatformAccountingReconciliationItem.closure_run_id
                        == closure_run_id,
                        PlatformAccountingReconciliationItem.reconciliation_key
                        == reconciliation_key,
                    )
                )
            ).scalar_one()

        if decision.safe_outcome in {"security_incident", "accounting_incident"}:
            if resolution_evidence_sha256 is None or resolution_evidence_ref is None:
                raise ValueError(
                    "PAY-14 incident creation requires durable incident evidence"
                )
            await self._open_incident(
                closure_run_id=closure_run_id,
                item_id=inserted,
                organization_id=organization_id,
                incident_type=decision.safe_outcome,
                incident_code=decision.reason_code,
                evidence_sha256=resolution_evidence_sha256,
                evidence_ref=resolution_evidence_ref,
                opened_at=last_checked_at,
            )

        return inserted

    async def mark_reconciling(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
    ) -> None:
        await self.set_tenant_context(organization_id)
        run = (
            await self._session.execute(
                select(PlatformAccountingClosureRun)
                .where(PlatformAccountingClosureRun.id == closure_run_id)
                .with_for_update()
            )
        ).scalar_one()
        if run.status == "collecting":
            run.status = "reconciling"
            run.version += 1
        await self._session.flush()

    async def refresh_counters(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
    ) -> None:
        await self.set_tenant_context(organization_id)
        run = (
            await self._session.execute(
                select(PlatformAccountingClosureRun)
                .where(PlatformAccountingClosureRun.id == closure_run_id)
                .with_for_update()
            )
        ).scalar_one()
        row = (
            await self._session.execute(
                select(
                    func.count(PlatformAccountingReconciliationItem.id),
                    func.count(PlatformAccountingReconciliationItem.id).filter(
                        PlatformAccountingReconciliationItem.mismatch_category.is_not(None)
                    ),
                    func.count(PlatformAccountingReconciliationItem.id).filter(
                        PlatformAccountingReconciliationItem.safe_outcome
                        == "retry_required"
                    ),
                    func.count(PlatformAccountingReconciliationItem.id).filter(
                        PlatformAccountingReconciliationItem.safe_outcome
                        == "manual_review_required"
                    ),
                    func.count(PlatformAccountingReconciliationItem.id).filter(
                        PlatformAccountingReconciliationItem.safe_outcome.in_(
                            ("security_incident", "accounting_incident")
                        )
                    ),
                    func.count(PlatformAccountingReconciliationItem.id).filter(
                        PlatformAccountingReconciliationItem.resolution_status
                        == "resolved"
                    ),
                ).where(
                    PlatformAccountingReconciliationItem.closure_run_id
                    == closure_run_id
                )
            )
        ).one()
        (
            run.observed_object_count,
            run.mismatch_count,
            run.retry_count,
            run.manual_review_count,
            run.incident_count,
            run.resolved_count,
        ) = tuple(int(value or 0) for value in row)
        run.version += 1
        await self._session.flush()

    async def mark_review_required(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
    ) -> None:
        await self.set_tenant_context(organization_id)
        run = (
            await self._session.execute(
                select(PlatformAccountingClosureRun)
                .where(PlatformAccountingClosureRun.id == closure_run_id)
                .with_for_update()
            )
        ).scalar_one()
        if run.status == "reconciling":
            run.status = "review_required"
            run.version += 1
        await self._session.flush()

    async def resolve_item(
        self,
        *,
        item_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        resolution_evidence_sha256: str,
        resolution_evidence_ref: str,
        resolved_at: datetime,
    ) -> None:
        await self.set_tenant_context(organization_id)
        item = (
            await self._session.execute(
                select(PlatformAccountingReconciliationItem)
                .where(PlatformAccountingReconciliationItem.id == item_id)
                .with_for_update()
            )
        ).scalar_one()
        if item.resolution_status == "resolved":
            return
        item.resolution_status = "resolved"
        item.resolution_evidence_sha256 = resolution_evidence_sha256
        item.resolution_evidence_ref = resolution_evidence_ref
        item.resolved_at = resolved_at
        item.last_checked_at = resolved_at
        item.version += 1
        await self._session.flush()

    async def resolve_incident(
        self,
        *,
        incident_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        resolution_code: str,
        resolved_at: datetime,
    ) -> None:
        await self.set_tenant_context(organization_id)
        incident = (
            await self._session.execute(
                select(PlatformAccountingIncident)
                .where(PlatformAccountingIncident.id == incident_id)
                .with_for_update()
            )
        ).scalar_one()
        if incident.status == "resolved":
            return
        incident.status = "resolved"
        incident.resolution_code = resolution_code
        incident.resolved_at = resolved_at
        await self._session.flush()

    async def mark_ready_to_close(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        evidence_manifest_sha256: str,
        evidence_manifest_ref: str,
        ready_at: datetime,
    ) -> None:
        await self.refresh_counters(
            closure_run_id=closure_run_id,
            organization_id=organization_id,
        )
        run = (
            await self._session.execute(
                select(PlatformAccountingClosureRun)
                .where(PlatformAccountingClosureRun.id == closure_run_id)
                .with_for_update()
            )
        ).scalar_one()
        if run.status not in {"reconciling", "review_required"}:
            raise ValueError("PAY-14 closure is not eligible for ready-to-close")
        run.evidence_manifest_sha256 = evidence_manifest_sha256
        run.evidence_manifest_ref = evidence_manifest_ref
        run.ready_at = ready_at
        run.status = "ready_to_close"
        run.version += 1
        await self._session.flush()

    async def close(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        closed_by: uuid.UUID,
        closed_at: datetime,
    ) -> None:
        await self.set_tenant_context(organization_id)
        run = (
            await self._session.execute(
                select(PlatformAccountingClosureRun)
                .where(PlatformAccountingClosureRun.id == closure_run_id)
                .with_for_update()
            )
        ).scalar_one()
        if run.status != "ready_to_close":
            raise ValueError("PAY-14 closure must be ready before close")
        run.status = "closed"
        run.closed_by = closed_by
        run.closed_at = closed_at
        run.version += 1
        await self._session.flush()

    async def _open_incident(
        self,
        *,
        closure_run_id: uuid.UUID,
        item_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        incident_type: str,
        incident_code: str,
        evidence_sha256: str,
        evidence_ref: str,
        opened_at: datetime,
    ) -> None:
        await self._session.execute(
            insert(PlatformAccountingIncident)
            .values(
                closure_run_id=closure_run_id,
                reconciliation_item_id=item_id,
                organization_id=organization_id,
                incident_type=incident_type,
                severity=(
                    "critical"
                    if incident_type == "security_incident"
                    else "warning"
                ),
                status="open",
                incident_code=incident_code,
                evidence_sha256=evidence_sha256,
                evidence_ref=evidence_ref,
                opened_at=opened_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_platform_accounting_incident_item_type"
            )
        )


def _resolution_status_for(safe_outcome: str) -> str:
    if safe_outcome == "auto_resolved_by_authoritative_evidence":
        return "resolved"
    if safe_outcome == "retry_required":
        return "retry_pending"
    if safe_outcome == "manual_review_required":
        return "under_review"
    if safe_outcome in {"security_incident", "accounting_incident"}:
        return "incident_open"
    raise ValueError(f"Unsupported PAY-14 safe outcome: {safe_outcome!r}")


