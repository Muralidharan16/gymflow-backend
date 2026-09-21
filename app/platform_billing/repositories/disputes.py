from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.models.disputes import (
    PlatformDispute,
    PlatformDisputeEvent,
    PlatformDisputeEvidence,
    PlatformDisputeFinancialEntry,
    PlatformFinancialExceptionCase,
)


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlatformDisputeRepository:
    """Tenant-scoped PAY-13 dispute persistence.

    Provider facts are applied idempotently. Financial closure is protected by
    deferred PostgreSQL constraints, so dispute state and its liability/loss
    entry must commit together.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def set_tenant_context(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )

    async def open_dispute(
        self,
        *,
        organization_id: uuid.UUID,
        dispute_id: uuid.UUID,
        payment_attempt_id: uuid.UUID,
        invoice_id: uuid.UUID,
        provider_release_id: uuid.UUID,
        provider_code: str,
        environment: str,
        external_dispute_ref: str,
        dispute_type: str,
        amount_minor: int,
        currency_code: str,
        reason_code: str | None,
        evidence_sha256: str,
        evidence_ref: str,
        occurred_at: datetime,
        source_type: str,
        source_id: uuid.UUID | None,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)

        created_id = (
            await self._session.execute(
                insert(PlatformDispute)
                .values(
                    id=dispute_id,
                    organization_id=organization_id,
                    payment_attempt_id=payment_attempt_id,
                    invoice_id=invoice_id,
                    provider_release_id=provider_release_id,
                    provider_code=provider_code,
                    environment=environment,
                    external_dispute_ref=external_dispute_ref,
                    dispute_type=dispute_type,
                    status="opened",
                    amount_minor=amount_minor,
                    currency_code=currency_code,
                    reason_code=reason_code,
                    financial_hold_active=True,
                    last_evidence_sha256=evidence_sha256,
                    last_evidence_ref=evidence_ref,
                    opened_at=occurred_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_disputes_provider_ref"
                )
                .returning(PlatformDispute.id)
            )
        ).scalar_one_or_none()

        if created_id is None:
            return (
                await self._session.execute(
                    select(PlatformDispute.id).where(
                        PlatformDispute.provider_code == provider_code,
                        PlatformDispute.environment == environment,
                        PlatformDispute.external_dispute_ref == external_dispute_ref,
                    )
                )
            ).scalar_one()

        self._session.add(
            PlatformDisputeFinancialEntry(
                organization_id=organization_id,
                dispute_id=created_id,
                payment_attempt_id=payment_attempt_id,
                entry_type="liability_recognized",
                amount_minor=amount_minor,
                currency_code=currency_code,
                evidence_sha256=evidence_sha256,
                evidence_ref=evidence_ref,
                effective_at=occurred_at,
            )
        )
        payload = {
            "dispute_type": dispute_type,
            "amount_minor": amount_minor,
            "currency_code": currency_code,
        }
        self._session.add(
            PlatformDisputeEvent(
                organization_id=organization_id,
                dispute_id=created_id,
                sequence_number=1,
                event_type="dispute_opened",
                from_status=None,
                to_status="opened",
                source_type=source_type,
                source_id=source_id,
                evidence_sha256=evidence_sha256,
                occurred_at=occurred_at,
                payload_json=payload,
                payload_sha256=_payload_sha256(payload),
            )
        )
        await self._session.flush()
        return created_id

    async def append_evidence(
        self,
        *,
        organization_id: uuid.UUID,
        dispute_id: uuid.UUID,
        evidence_kind: str,
        evidence_sha256: str,
        evidence_ref: str,
        source_type: str,
        source_id: uuid.UUID | None,
        metadata_json: dict[str, Any],
        observed_at: datetime,
        submitted_at: datetime | None = None,
        provider_ack_ref: str | None = None,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)
        inserted = (
            await self._session.execute(
                insert(PlatformDisputeEvidence)
                .values(
                    organization_id=organization_id,
                    dispute_id=dispute_id,
                    evidence_kind=evidence_kind,
                    evidence_sha256=evidence_sha256,
                    evidence_ref=evidence_ref,
                    source_type=source_type,
                    source_id=source_id,
                    metadata_json=metadata_json,
                    observed_at=observed_at,
                    submitted_at=submitted_at,
                    provider_ack_ref=provider_ack_ref,
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_dispute_evidence_sha"
                )
                .returning(PlatformDisputeEvidence.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted
        return (
            await self._session.execute(
                select(PlatformDisputeEvidence.id).where(
                    PlatformDisputeEvidence.dispute_id == dispute_id,
                    PlatformDisputeEvidence.evidence_sha256 == evidence_sha256,
                )
            )
        ).scalar_one()

    async def transition(
        self,
        *,
        organization_id: uuid.UUID,
        dispute_id: uuid.UUID,
        target_status: str,
        evidence_sha256: str,
        evidence_ref: str,
        occurred_at: datetime,
        source_type: str,
        source_id: uuid.UUID | None,
        provider_decision_ref: str | None = None,
        financial_entry_type: str | None = None,
        close: bool = False,
    ) -> None:
        await self.set_tenant_context(organization_id)
        dispute = (
            await self._session.execute(
                select(PlatformDispute)
                .where(
                    PlatformDispute.id == dispute_id,
                    PlatformDispute.organization_id == organization_id,
                )
                .with_for_update()
            )
        ).scalar_one()

        old_status = dispute.status
        if old_status == target_status:
            return

        dispute.status = target_status
        dispute.last_evidence_sha256 = evidence_sha256
        dispute.last_evidence_ref = evidence_ref
        dispute.version += 1

        if target_status == "submitted":
            dispute.submitted_at = occurred_at
        elif target_status in {"won", "lost"}:
            dispute.provider_decision_ref = provider_decision_ref
            dispute.decided_at = occurred_at
            dispute.financial_hold_active = False
            dispute.hold_released_at = occurred_at
        elif target_status == "closed" or close:
            dispute.closed_at = occurred_at

        if financial_entry_type is not None:
            self._session.add(
                PlatformDisputeFinancialEntry(
                    organization_id=organization_id,
                    dispute_id=dispute_id,
                    payment_attempt_id=dispute.payment_attempt_id,
                    entry_type=financial_entry_type,
                    amount_minor=dispute.amount_minor,
                    currency_code=dispute.currency_code,
                    evidence_sha256=evidence_sha256,
                    evidence_ref=evidence_ref,
                    effective_at=occurred_at,
                )
            )

        next_sequence = (
            await self._session.execute(
                select(func.coalesce(func.max(PlatformDisputeEvent.sequence_number), 0) + 1)
                .where(PlatformDisputeEvent.dispute_id == dispute_id)
            )
        ).scalar_one()
        payload = {
            "from_status": old_status,
            "to_status": target_status,
            "provider_decision_ref": provider_decision_ref,
        }
        self._session.add(
            PlatformDisputeEvent(
                organization_id=organization_id,
                dispute_id=dispute_id,
                sequence_number=next_sequence,
                event_type=f"dispute_{target_status}",
                from_status=old_status,
                to_status=target_status,
                source_type=source_type,
                source_id=source_id,
                evidence_sha256=evidence_sha256,
                occurred_at=occurred_at,
                payload_json=payload,
                payload_sha256=_payload_sha256(payload),
            )
        )
        await self._session.flush()

    async def record_loss_reversal(
        self,
        *,
        organization_id: uuid.UUID,
        dispute_id: uuid.UUID,
        evidence_sha256: str,
        evidence_ref: str,
        occurred_at: datetime,
        source_type: str,
        source_id: uuid.UUID | None,
    ) -> None:
        await self.set_tenant_context(organization_id)
        dispute = (
            await self._session.execute(
                select(PlatformDispute)
                .where(
                    PlatformDispute.id == dispute_id,
                    PlatformDispute.organization_id == organization_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        self._session.add(
            PlatformDisputeFinancialEntry(
                organization_id=organization_id,
                dispute_id=dispute_id,
                payment_attempt_id=dispute.payment_attempt_id,
                entry_type="loss_reversed",
                amount_minor=dispute.amount_minor,
                currency_code=dispute.currency_code,
                evidence_sha256=evidence_sha256,
                evidence_ref=evidence_ref,
                effective_at=occurred_at,
            )
        )
        next_sequence = (
            await self._session.execute(
                select(func.coalesce(func.max(PlatformDisputeEvent.sequence_number), 0) + 1)
                .where(PlatformDisputeEvent.dispute_id == dispute_id)
            )
        ).scalar_one()
        payload = {"financial_entry_type": "loss_reversed"}
        self._session.add(
            PlatformDisputeEvent(
                organization_id=organization_id,
                dispute_id=dispute_id,
                sequence_number=next_sequence,
                event_type="chargeback_reversed",
                from_status=dispute.status,
                to_status=dispute.status,
                source_type=source_type,
                source_id=source_id,
                evidence_sha256=evidence_sha256,
                occurred_at=occurred_at,
                payload_json=payload,
                payload_sha256=_payload_sha256(payload),
            )
        )
        await self._session.flush()

    async def open_exception(
        self,
        *,
        exception_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        provider_code: str,
        environment: str,
        exception_type: str,
        severity: str,
        external_object_type: str,
        external_object_ref: str,
        amount_minor: int | None,
        currency_code: str | None,
        evidence_sha256: str,
        evidence_ref: str,
        source_type: str,
        source_id: uuid.UUID | None,
        detected_at: datetime,
        payment_attempt_id: uuid.UUID | None = None,
        invoice_id: uuid.UUID | None = None,
        refund_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        if organization_id is not None:
            await self.set_tenant_context(organization_id)

        inserted = (
            await self._session.execute(
                insert(PlatformFinancialExceptionCase)
                .values(
                    id=exception_id,
                    organization_id=organization_id,
                    payment_attempt_id=payment_attempt_id,
                    invoice_id=invoice_id,
                    refund_id=refund_id,
                    provider_code=provider_code,
                    environment=environment,
                    exception_type=exception_type,
                    status="quarantined",
                    severity=severity,
                    external_object_type=external_object_type,
                    external_object_ref=external_object_ref,
                    amount_minor=amount_minor,
                    currency_code=currency_code,
                    initial_evidence_sha256=evidence_sha256,
                    initial_evidence_ref=evidence_ref,
                    source_type=source_type,
                    source_id=source_id,
                    manual_review_required=True,
                    automatic_financial_mutation_allowed=False,
                    detected_at=detected_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_platform_financial_exception_fact"
                )
                .returning(PlatformFinancialExceptionCase.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted

        return (
            await self._session.execute(
                select(PlatformFinancialExceptionCase.id).where(
                    PlatformFinancialExceptionCase.provider_code == provider_code,
                    PlatformFinancialExceptionCase.environment == environment,
                    PlatformFinancialExceptionCase.exception_type == exception_type,
                    PlatformFinancialExceptionCase.external_object_ref == external_object_ref,
                    PlatformFinancialExceptionCase.initial_evidence_sha256 == evidence_sha256,
                )
            )
        ).scalar_one()
