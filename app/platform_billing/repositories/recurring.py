from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.models.recurring import (
    PlatformDunningCase,
    PlatformNotificationDelivery,
    PlatformRecurringBillingJob,
)


@dataclass(frozen=True)
class RecurringJobClaim:
    job_id: uuid.UUID
    organization_id: uuid.UUID
    subscription_id: uuid.UUID
    invoice_id: uuid.UUID | None
    attempt_number: int
    max_attempts: int
    worker_id: uuid.UUID
    lease_fence: int
    lease_until: datetime
    claimed: bool


class PlatformRecurringBillingRepository:
    """Tenant-scoped durable recurring-work repository.

    It never scans across organizations. A caller must resolve an exact tenant
    before using it. Lease fencing prevents a stale execution from completing
    after another worker reclaimed the same recurring period.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def set_tenant_context(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )

    async def schedule_period(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
        period_start: datetime,
        period_end: datetime,
        run_at: datetime,
        max_attempts: int,
    ) -> uuid.UUID:
        await self.set_tenant_context(organization_id)
        statement = (
            insert(PlatformRecurringBillingJob)
            .values(
                organization_id=organization_id,
                subscription_id=subscription_id,
                period_start=period_start,
                period_end=period_end,
                run_at=run_at,
                max_attempts=max_attempts,
                status="scheduled",
            )
            .on_conflict_do_nothing(
                constraint="uq_platform_recurring_jobs_period",
            )
            .returning(PlatformRecurringBillingJob.id)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return inserted

        existing = (
            await self._session.execute(
                select(PlatformRecurringBillingJob.id).where(
                    PlatformRecurringBillingJob.organization_id == organization_id,
                    PlatformRecurringBillingJob.subscription_id == subscription_id,
                    PlatformRecurringBillingJob.period_start == period_start,
                    PlatformRecurringBillingJob.period_end == period_end,
                )
            )
        ).scalar_one()
        return existing

    async def claim_due(
        self,
        *,
        organization_id: uuid.UUID,
        worker_id: uuid.UUID,
        now: datetime,
        lease_seconds: int = 300,
    ) -> RecurringJobClaim | None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("PAY-12 lease_seconds must be between 1 and 3600")

        await self.set_tenant_context(organization_id)
        lease_until = now + timedelta(seconds=lease_seconds)

        fresh_due = and_(
            PlatformRecurringBillingJob.status.in_(("scheduled", "retry")),
            PlatformRecurringBillingJob.run_at <= now,
            (
                PlatformRecurringBillingJob.next_attempt_at.is_(None)
                | (PlatformRecurringBillingJob.next_attempt_at <= now)
            ),
            (
                PlatformRecurringBillingJob.lease_until.is_(None)
                | (PlatformRecurringBillingJob.lease_until <= now)
            ),
            PlatformRecurringBillingJob.attempt_count
            < PlatformRecurringBillingJob.max_attempts,
        )
        reconciliation_due = and_(
            PlatformRecurringBillingJob.status == "awaiting_reconciliation",
            PlatformRecurringBillingJob.run_at <= now,
            (
                PlatformRecurringBillingJob.next_attempt_at.is_(None)
                | (PlatformRecurringBillingJob.next_attempt_at <= now)
            ),
            (
                PlatformRecurringBillingJob.lease_until.is_(None)
                | (PlatformRecurringBillingJob.lease_until <= now)
            ),
        )
        expired_processing = and_(
            PlatformRecurringBillingJob.status == "processing",
            PlatformRecurringBillingJob.lease_until.is_not(None),
            PlatformRecurringBillingJob.lease_until <= now,
        )

        candidate = (
            await self._session.execute(
                select(PlatformRecurringBillingJob)
                .where(
                    PlatformRecurringBillingJob.organization_id == organization_id,
                    or_(fresh_due, reconciliation_due, expired_processing),
                )
                .order_by(
                    PlatformRecurringBillingJob.run_at,
                    PlatformRecurringBillingJob.created_at,
                    PlatformRecurringBillingJob.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None

        previous_status = candidate.status
        reclaim = previous_status == "processing"
        consumes_provider_attempt = previous_status in ("scheduled", "retry")

        candidate.status = "processing"
        candidate.lease_owner = worker_id
        candidate.lease_until = lease_until
        candidate.lease_fence += 1
        if consumes_provider_attempt:
            candidate.attempt_count += 1
        elif reclaim:
            # A crash/reclaim never consumes a second provider attempt,
            # including when the crashed execution was the final allowed one.
            pass
        candidate.version += 1
        await self._session.flush()
        return RecurringJobClaim(
            job_id=candidate.id,
            organization_id=candidate.organization_id,
            subscription_id=candidate.subscription_id,
            invoice_id=candidate.invoice_id,
            attempt_number=candidate.attempt_count,
            max_attempts=candidate.max_attempts,
            worker_id=worker_id,
            lease_fence=candidate.lease_fence,
            lease_until=lease_until,
            claimed=True,
        )

    async def complete(
        self,
        *,
        claim: RecurringJobClaim,
        invoice_id: uuid.UUID,
        payment_attempt_id: uuid.UUID | None,
        evidence_sha256: str,
        evidence_ref: str,
        now: datetime,
    ) -> None:
        await self.set_tenant_context(claim.organization_id)
        result = await self._session.execute(
            update(PlatformRecurringBillingJob)
            .where(
                PlatformRecurringBillingJob.id == claim.job_id,
                PlatformRecurringBillingJob.organization_id == claim.organization_id,
                PlatformRecurringBillingJob.status == "processing",
                PlatformRecurringBillingJob.lease_owner == claim.worker_id,
                PlatformRecurringBillingJob.lease_fence == claim.lease_fence,
                PlatformRecurringBillingJob.lease_until > now,
            )
            .values(
                status="completed",
                invoice_id=invoice_id,
                last_payment_attempt_id=payment_attempt_id,
                last_evidence_sha256=evidence_sha256,
                last_evidence_ref=evidence_ref,
                lease_owner=None,
                lease_until=None,
                completed_at=now,
                next_attempt_at=None,
                last_error_code=None,
                version=PlatformRecurringBillingJob.version + 1,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("PAY-12 recurring job lease lost before completion")

    async def release_for_retry(
        self,
        *,
        claim: RecurringJobClaim,
        next_attempt_at: datetime,
        error_code: str,
        evidence_sha256: str | None,
        evidence_ref: str | None,
        now: datetime,
        awaiting_reconciliation: bool = False,
    ) -> None:
        if (
            not awaiting_reconciliation
            and claim.attempt_number >= claim.max_attempts
        ):
            raise ValueError(
                "PAY-12 retry budget exhausted; final provider attempt cannot be rescheduled"
            )

        await self.set_tenant_context(claim.organization_id)
        next_status = "awaiting_reconciliation" if awaiting_reconciliation else "retry"
        result = await self._session.execute(
            update(PlatformRecurringBillingJob)
            .where(
                PlatformRecurringBillingJob.id == claim.job_id,
                PlatformRecurringBillingJob.organization_id == claim.organization_id,
                PlatformRecurringBillingJob.status == "processing",
                PlatformRecurringBillingJob.lease_owner == claim.worker_id,
                PlatformRecurringBillingJob.lease_fence == claim.lease_fence,
                PlatformRecurringBillingJob.lease_until > now,
            )
            .values(
                status=next_status,
                next_attempt_at=next_attempt_at,
                last_error_code=error_code,
                last_evidence_sha256=evidence_sha256,
                last_evidence_ref=evidence_ref,
                lease_owner=None,
                lease_until=None,
                version=PlatformRecurringBillingJob.version + 1,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("PAY-12 recurring job lease lost before retry release")


class PlatformDunningRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_open_for_invoice(
        self,
        *,
        organization_id: uuid.UUID,
        invoice_id: uuid.UUID,
        for_update: bool = False,
    ) -> PlatformDunningCase | None:
        statement = select(PlatformDunningCase).where(
            PlatformDunningCase.organization_id == organization_id,
            PlatformDunningCase.invoice_id == invoice_id,
            PlatformDunningCase.status.in_(("open", "suspended")),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def enqueue_notification(
        self,
        *,
        organization_id: uuid.UUID,
        dunning_case_id: uuid.UUID | None,
        notification_type: str,
        policy_code: str,
        channel: str,
        recipient_hash: str,
        scheduled_at: datetime,
        dedupe_key: str,
    ) -> uuid.UUID:
        statement = (
            insert(PlatformNotificationDelivery)
            .values(
                organization_id=organization_id,
                dunning_case_id=dunning_case_id,
                notification_type=notification_type,
                policy_code=policy_code,
                channel=channel,
                recipient_hash=recipient_hash,
                scheduled_at=scheduled_at,
                dedupe_key=dedupe_key,
                status="queued",
            )
            .on_conflict_do_nothing(
                constraint="uq_platform_notification_deliveries_dedupe",
            )
            .returning(PlatformNotificationDelivery.id)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return inserted
        return (
            await self._session.execute(
                select(PlatformNotificationDelivery.id).where(
                    PlatformNotificationDelivery.organization_id == organization_id,
                    PlatformNotificationDelivery.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one()
