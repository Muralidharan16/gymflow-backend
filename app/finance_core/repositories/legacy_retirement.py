from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class LegacyBatchSnapshot:
    id: uuid.UUID
    organization_id: uuid.UUID
    status: str
    manifest_sha256: str | None


class FinanceLegacyRetirementRepository:
    """PAY-15 configuration-only capability client.

    The repository has no direct INSERT/UPDATE/DELETE authority on PAY-15 tables.
    All mutation goes through app_secure functions granted only to the
    finance_config_runtime capability.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _prepare(self, organization_id: uuid.UUID) -> None:
        member = (
            await self._session.execute(
                text(
                    """
                    SELECT pg_catalog.pg_has_role(
                        session_user, 'finance_config_runtime', 'MEMBER'
                    )
                    """
                )
            )
        ).scalar_one()
        if member is not True:
            raise PermissionError(
                "PAY-15 migration requires finance_config_runtime"
            )
        await self._session.execute(
            text(
                """
                SELECT pg_catalog.set_config(
                    'app.current_org_id', :organization_id, true
                )
                """
            ),
            {"organization_id": str(organization_id)},
        )

    async def capture_inventory(
        self,
        *,
        organization_id: uuid.UUID,
        scope_key: str,
        actor_ref: str,
        evidence_ref: str,
    ) -> uuid.UUID:
        await self._prepare(organization_id)
        return (
            await self._session.execute(
                text(
                    """
                    SELECT app_secure.pay15_capture_inventory(
                        :organization_id, :scope_key, :actor_ref, :evidence_ref
                    )
                    """
                ),
                {
                    "organization_id": organization_id,
                    "scope_key": scope_key,
                    "actor_ref": actor_ref,
                    "evidence_ref": evidence_ref,
                },
            )
        ).scalar_one()

    async def begin_reconciliation(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
    ) -> None:
        await self._prepare(organization_id)
        await self._session.execute(
            text(
                "SELECT app_secure.pay15_begin_reconciliation(:batch_id)"
            ),
            {"batch_id": batch_id},
        )

    async def record_invoice_disposition(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_invoice_id: uuid.UUID,
        disposition: str,
        finance_invoice_id: uuid.UUID | None,
        source_currency_code: str,
        actor_ref: str,
        evidence_ref: str,
    ) -> uuid.UUID:
        await self._prepare(organization_id)
        return (
            await self._session.execute(
                text(
                    """
                    SELECT app_secure.pay15_record_invoice_disposition(
                        :batch_id, :legacy_invoice_id, :disposition,
                        :finance_invoice_id, :source_currency_code,
                        :actor_ref, :evidence_ref
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "legacy_invoice_id": legacy_invoice_id,
                    "disposition": disposition,
                    "finance_invoice_id": finance_invoice_id,
                    "source_currency_code": source_currency_code,
                    "actor_ref": actor_ref,
                    "evidence_ref": evidence_ref,
                },
            )
        ).scalar_one()

    async def record_payment_disposition(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_payment_id: uuid.UUID,
        disposition: str,
        finance_payment_id: uuid.UUID | None,
        source_currency_code: str,
        actor_ref: str,
        evidence_ref: str,
    ) -> uuid.UUID:
        await self._prepare(organization_id)
        return (
            await self._session.execute(
                text(
                    """
                    SELECT app_secure.pay15_record_payment_disposition(
                        :batch_id, :legacy_payment_id, :disposition,
                        :finance_payment_id, :source_currency_code,
                        :actor_ref, :evidence_ref
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "legacy_payment_id": legacy_payment_id,
                    "disposition": disposition,
                    "finance_payment_id": finance_payment_id,
                    "source_currency_code": source_currency_code,
                    "actor_ref": actor_ref,
                    "evidence_ref": evidence_ref,
                },
            )
        ).scalar_one()

    async def record_subscription_link(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        legacy_subscription_id: uuid.UUID,
        disposition: str,
        modern_subscription_term_id: uuid.UUID | None,
        actor_ref: str,
        evidence_ref: str,
    ) -> uuid.UUID:
        await self._prepare(organization_id)
        return (
            await self._session.execute(
                text(
                    """
                    SELECT app_secure.pay15_record_subscription_link(
                        :batch_id, :legacy_subscription_id, :disposition,
                        :modern_subscription_term_id, :actor_ref, :evidence_ref
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "legacy_subscription_id": legacy_subscription_id,
                    "disposition": disposition,
                    "modern_subscription_term_id": modern_subscription_term_id,
                    "actor_ref": actor_ref,
                    "evidence_ref": evidence_ref,
                },
            )
        ).scalar_one()

    async def certify_ready(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        actor_ref: str,
        evidence_ref: str,
    ) -> str:
        await self._prepare(organization_id)
        return (
            await self._session.execute(
                text(
                    """
                    SELECT app_secure.pay15_certify_ready(
                        :batch_id, :actor_ref, :evidence_ref
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "actor_ref": actor_ref,
                    "evidence_ref": evidence_ref,
                },
            )
        ).scalar_one()

    async def activate_cutover(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        expected_manifest_sha256: str,
        actor_ref: str,
        evidence_ref: str,
    ) -> None:
        await self._prepare(organization_id)
        await self._session.execute(
            text(
                """
                SELECT app_secure.pay15_activate_cutover(
                    :batch_id, :expected_manifest_sha256,
                    :actor_ref, :evidence_ref
                )
                """
            ),
            {
                "batch_id": batch_id,
                "expected_manifest_sha256": expected_manifest_sha256,
                "actor_ref": actor_ref,
                "evidence_ref": evidence_ref,
            },
        )

    async def enter_rollback_hold(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: uuid.UUID,
        actor_ref: str,
        evidence_sha256: str,
        evidence_ref: str,
    ) -> None:
        await self._prepare(organization_id)
        await self._session.execute(
            text(
                """
                SELECT app_secure.pay15_enter_rollback_hold(
                    :batch_id, :actor_ref, :evidence_sha256, :evidence_ref
                )
                """
            ),
            {
                "batch_id": batch_id,
                "actor_ref": actor_ref,
                "evidence_sha256": evidence_sha256,
                "evidence_ref": evidence_ref,
            },
        )
