from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.platform_billing.domain.accounting_reconciliation import (
    FinancialObservation,
    ReconciliationDecision,
    ThreeWayReconciliationInput,
    classify_three_way_reconciliation,
    reconciliation_key,
)
from app.platform_billing.repositories.accounting_reconciliation import (
    PlatformAccountingReconciliationRepository,
)


@dataclass(frozen=True)
class ThreeWayEvidenceBundle:
    local: FinancialObservation | None
    provider: FinancialObservation | None
    settlement: FinancialObservation | None
    local_evidence_kind: str = "local_snapshot"
    provider_evidence_kind: str = "provider_api"
    settlement_evidence_kind: str = "provider_settlement"


@dataclass(frozen=True)
class ReconciliationPersistResult:
    item_id: uuid.UUID
    decision: ReconciliationDecision


class PlatformAccountingReconciliationService:
    """PAY-14 three-way reconciliation service.

    The service records evidence and a reconciliation decision only. It has no
    capability to update payment, refund, dispute, chargeback, settlement, or
    accounting money state.
    """

    def __init__(self, repository: PlatformAccountingReconciliationRepository):
        self._repository = repository

    async def reconcile_object(
        self,
        *,
        item_id: uuid.UUID,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        provider_code: str,
        environment: str,
        object_type: str,
        external_object_ref: str,
        evidence: ThreeWayEvidenceBundle,
        provider_duplicate_count: int = 1,
        provider_object_known: bool = True,
        settlement_expected: bool = True,
        local_object_id: uuid.UUID | None = None,
        checked_at: datetime,
    ) -> ReconciliationPersistResult:
        data = ThreeWayReconciliationInput(
            object_type=object_type,
            local=evidence.local,
            provider=evidence.provider,
            settlement=evidence.settlement,
            provider_duplicate_count=provider_duplicate_count,
            provider_object_known=provider_object_known,
            settlement_expected=settlement_expected,
        )
        decision = classify_three_way_reconciliation(data)

        local_evidence_id = await self._persist_optional_evidence(
            closure_run_id=closure_run_id,
            organization_id=organization_id,
            provider_code=provider_code,
            environment=environment,
            observation=evidence.local,
            evidence_kind=evidence.local_evidence_kind,
            local_object_id=local_object_id,
        )
        provider_evidence_id = await self._persist_optional_evidence(
            closure_run_id=closure_run_id,
            organization_id=organization_id,
            provider_code=provider_code,
            environment=environment,
            observation=evidence.provider,
            evidence_kind=evidence.provider_evidence_kind,
            local_object_id=local_object_id,
        )
        settlement_evidence_id = await self._persist_optional_evidence(
            closure_run_id=closure_run_id,
            organization_id=organization_id,
            provider_code=provider_code,
            environment=environment,
            observation=evidence.settlement,
            evidence_kind=evidence.settlement_evidence_kind,
            local_object_id=local_object_id,
        )

        resolution_observation = (
            evidence.settlement or evidence.provider or evidence.local
        )
        resolution_sha = None
        resolution_ref = None
        if decision.safe_outcome in {
            "auto_resolved_by_authoritative_evidence",
            "security_incident",
            "accounting_incident",
        }:
            if resolution_observation is None:
                raise ValueError("PAY-14 decision requires durable resolution evidence")
            resolution_sha = resolution_observation.evidence_sha256
            resolution_ref = resolution_observation.evidence_ref

        persisted_item_id = await self._repository.record_decision(
            item_id=item_id,
            closure_run_id=closure_run_id,
            organization_id=organization_id,
            object_type=object_type,
            reconciliation_key=reconciliation_key(
                provider_code=provider_code,
                environment=environment,
                object_type=object_type,
                external_object_ref=external_object_ref,
            ),
            decision=decision,
            first_detected_at=checked_at,
            last_checked_at=checked_at,
            local_evidence_id=local_evidence_id,
            provider_evidence_id=provider_evidence_id,
            settlement_evidence_id=settlement_evidence_id,
            local_object_id=local_object_id,
            resolution_evidence_sha256=resolution_sha,
            resolution_evidence_ref=resolution_ref,
        )
        return ReconciliationPersistResult(
            item_id=persisted_item_id,
            decision=decision,
        )

    async def _persist_optional_evidence(
        self,
        *,
        closure_run_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        provider_code: str,
        environment: str,
        observation: FinancialObservation | None,
        evidence_kind: str,
        local_object_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        if observation is None:
            return None
        return await self._repository.append_evidence(
            closure_run_id=closure_run_id,
            organization_id=organization_id,
            provider_code=provider_code,
            environment=environment,
            observation=observation,
            evidence_kind=evidence_kind,
            local_object_id=local_object_id,
        )
