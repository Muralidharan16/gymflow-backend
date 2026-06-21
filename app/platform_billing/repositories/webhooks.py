from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.webhooks import (
    VerifiedWebhook,
    WebhookDuplicateConflict,
    WebhookClaimLost,
    WebhookInboxSnapshot,
)
from app.platform_billing.models.webhook import PlatformWebhookInbox


class PlatformWebhookInboxRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def accept(
        self,
        *,
        verified: VerifiedWebhook,
        payload_sha256: str,
        encrypted_payload_ref: str,
    ) -> WebhookInboxSnapshot:
        statement = (
            insert(PlatformWebhookInbox)
            .values(
                provider_code=verified.provider_code,
                provider_event_id=verified.provider_event_id,
                payload_sha256=payload_sha256,
                encrypted_payload_ref=encrypted_payload_ref,
                normalized_event_type=verified.normalized_event_type,
                processing_status="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing()
            .returning(PlatformWebhookInbox)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_snapshot(row), was_created=True)

        existing = await self.get_by_provider_event(
            provider_code=verified.provider_code,
            provider_event_id=verified.provider_event_id,
            for_update=True,
        )
        if existing is None:
            raise WebhookDuplicateConflict("Webhook event conflicted but no existing inbox row was visible")
        if existing.payload_sha256 != payload_sha256:
            raise WebhookDuplicateConflict("Webhook event id was reused with a different payload hash")
        return replace(existing, duplicate_replay=True)

    async def get_by_provider_event(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
        for_update: bool = False,
    ) -> WebhookInboxSnapshot | None:
        statement = select(PlatformWebhookInbox).where(
            PlatformWebhookInbox.provider_code == provider_code,
            PlatformWebhookInbox.provider_event_id == provider_event_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _snapshot(row) if row is not None else None

    async def get_by_id(
        self,
        inbox_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WebhookInboxSnapshot | None:
        statement = select(PlatformWebhookInbox).where(PlatformWebhookInbox.id == inbox_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _snapshot(row) if row is not None else None

    async def claim_for_processing(
        self,
        inbox_id: uuid.UUID,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> WebhookInboxSnapshot:
        statement = (
            update(PlatformWebhookInbox)
            .where(
                PlatformWebhookInbox.id == inbox_id,
                (
                    PlatformWebhookInbox.processing_status.in_(("pending", "failed_retryable"))
                    | (
                        (PlatformWebhookInbox.processing_status == "processing")
                        & (PlatformWebhookInbox.updated_at < stale_before)
                    )
                ),
            )
            .values(
                processing_status="processing",
                attempt_count=PlatformWebhookInbox.attempt_count + 1,
                updated_at=now,
            )
            .returning(PlatformWebhookInbox)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_snapshot(row), processing_claimed=True)

        current = await self.get_by_id(inbox_id)
        if current is None:
            raise WebhookDuplicateConflict("Webhook inbox row was not found for processing claim")
        return current

    async def assert_claim_owned(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
    ) -> WebhookInboxSnapshot:
        statement = (
            select(PlatformWebhookInbox)
            .where(
                PlatformWebhookInbox.id == inbox_id,
                PlatformWebhookInbox.processing_status == "processing",
                PlatformWebhookInbox.attempt_count == expected_attempt_count,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise WebhookClaimLost("Webhook processing claim was lost")
        return _snapshot(row)

    async def mark_processed(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        now: datetime,
    ) -> WebhookInboxSnapshot:
        return await self._mark(
            inbox_id,
            expected_attempt_count=expected_attempt_count,
            processing_status="processed",
            error_classification=None,
            error_detail_safe=None,
            processed=True,
            now=now,
        )

    async def mark_ignored(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        error_classification: str,
        error_detail_safe: str | None = None,
        now: datetime,
    ) -> WebhookInboxSnapshot:
        return await self._mark(
            inbox_id,
            expected_attempt_count=expected_attempt_count,
            processing_status="ignored",
            error_classification=error_classification,
            error_detail_safe=error_detail_safe,
            processed=True,
            now=now,
        )

    async def mark_failed_retryable(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        error_classification: str,
        error_detail_safe: str | None = None,
        now: datetime,
    ) -> WebhookInboxSnapshot:
        return await self._mark(
            inbox_id,
            expected_attempt_count=expected_attempt_count,
            processing_status="failed_retryable",
            error_classification=error_classification,
            error_detail_safe=error_detail_safe,
            processed=False,
            now=now,
        )

    async def mark_failed_final(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        error_classification: str,
        error_detail_safe: str | None = None,
        now: datetime,
    ) -> WebhookInboxSnapshot:
        return await self._mark(
            inbox_id,
            expected_attempt_count=expected_attempt_count,
            processing_status="failed_final",
            error_classification=error_classification,
            error_detail_safe=error_detail_safe,
            processed=True,
            now=now,
        )

    async def _mark(
        self,
        inbox_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        processing_status: str,
        error_classification: str | None,
        error_detail_safe: str | None,
        processed: bool,
        now: datetime,
    ) -> WebhookInboxSnapshot:
        values = {
            "processing_status": processing_status,
            "error_classification": error_classification,
            "error_detail_safe": error_detail_safe,
            "updated_at": now,
        }
        if processed:
            values["processed_at"] = now
        statement = (
            update(PlatformWebhookInbox)
            .where(
                PlatformWebhookInbox.id == inbox_id,
                PlatformWebhookInbox.processing_status == "processing",
                PlatformWebhookInbox.attempt_count == expected_attempt_count,
            )
            .values(**values)
            .returning(PlatformWebhookInbox)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise WebhookClaimLost("Webhook processing claim was lost")
        await self._session.flush()
        return _snapshot(row)


def _snapshot(row: PlatformWebhookInbox) -> WebhookInboxSnapshot:
    return WebhookInboxSnapshot(
        id=row.id,
        provider_code=row.provider_code,
        provider_event_id=row.provider_event_id,
        payload_sha256=row.payload_sha256,
        encrypted_payload_ref=row.encrypted_payload_ref,
        normalized_event_type=row.normalized_event_type,
        processing_status=row.processing_status,
        attempt_count=row.attempt_count,
        received_at=row.received_at,
        processed_at=row.processed_at,
        error_classification=row.error_classification,
        error_detail_safe=row.error_detail_safe,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
