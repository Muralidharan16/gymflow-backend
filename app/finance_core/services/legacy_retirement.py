from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.repositories.legacy_retirement import (
    FinanceLegacyRetirementRepository,
)


class FinanceLegacyRetirementService:
    """PAY-15 controlled retirement workflow.

    This service is intentionally not wired to FastAPI. It must run through a
    finance_config_runtime-authorized database identity and all authoritative
    validation remains database-owned.
    """

    def __init__(self, session: AsyncSession):
        self._repo = FinanceLegacyRetirementRepository(session)

    async def start(
        self,
        *,
        organization_id: uuid.UUID,
        scope_key: str,
        actor_ref: str,
        evidence_ref: str,
    ) -> uuid.UUID:
        batch_id = await self._repo.capture_inventory(
            organization_id=organization_id,
            scope_key=scope_key,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )
        await self._repo.begin_reconciliation(
            organization_id=organization_id,
            batch_id=batch_id,
        )
        return batch_id

    async def disposition_invoice(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_invoice_id: uuid.UUID,
        disposition: str,
        source_currency_code: str,
        actor_ref: str,
        evidence_ref: str,
        finance_invoice_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return await self._repo.record_invoice_disposition(
            organization_id=organization_id,
            batch_id=batch_id,
            legacy_invoice_id=legacy_invoice_id,
            disposition=disposition,
            finance_invoice_id=finance_invoice_id,
            source_currency_code=source_currency_code,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )

    async def disposition_payment(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_payment_id: uuid.UUID,
        disposition: str,
        source_currency_code: str,
        actor_ref: str,
        evidence_ref: str,
        finance_payment_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return await self._repo.record_payment_disposition(
            organization_id=organization_id,
            batch_id=batch_id,
            legacy_payment_id=legacy_payment_id,
            disposition=disposition,
            finance_payment_id=finance_payment_id,
            source_currency_code=source_currency_code,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )

    async def preserve_subscription_link(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_subscription_id: uuid.UUID,
        disposition: str,
        actor_ref: str,
        evidence_ref: str,
        modern_subscription_term_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return await self._repo.record_subscription_link(
            organization_id=organization_id,
            batch_id=batch_id,
            legacy_subscription_id=legacy_subscription_id,
            disposition=disposition,
            modern_subscription_term_id=modern_subscription_term_id,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )

    async def certify_and_cutover(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        actor_ref: str,
        evidence_ref: str,
    ) -> str:
        manifest = await self._repo.certify_ready(
            organization_id=organization_id,
            batch_id=batch_id,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )
        await self._repo.activate_cutover(
            organization_id=organization_id,
            batch_id=batch_id,
            expected_manifest_sha256=manifest,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
        )
        return manifest

    async def rollback_hold(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        actor_ref: str,
        evidence_sha256: str,
        evidence_ref: str,
    ) -> None:
        await self._repo.enter_rollback_hold(
            organization_id=organization_id,
            batch_id=batch_id,
            actor_ref=actor_ref,
            evidence_sha256=evidence_sha256,
            evidence_ref=evidence_ref,
        )
