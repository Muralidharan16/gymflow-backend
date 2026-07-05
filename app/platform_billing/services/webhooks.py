from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.provider_operations import (
    ProviderCallResult,
    ProviderOperationResult,
    ProviderOutcomeKind,
    result_for_outcome,
)
from app.platform_billing.domain.webhooks import (
    NormalizedWebhookEvent,
    WebhookAcceptanceResult,
    WebhookDuplicateConflict,
    WebhookInboxAcceptanceFailure,
    WebhookInboxSnapshot,
    WebhookProcessingClaim,
    WebhookProcessingResult,
    WebhookUnsupportedEvent,
    WebhookClaimLost,
    compute_webhook_payload_sha256,
)
from app.platform_billing.models.provider import PlatformProviderCustomer
from app.platform_billing.repositories.provider_operations import (
    PlatformProviderOperationRepository,
)
from app.platform_billing.repositories.webhooks import PlatformWebhookInboxRepository
from app.platform_billing.webhooks.contracts import (
    EncryptedWebhookPayloadStore,
    WebhookSignatureVerifier,
)


SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]
Clock = Callable[[], datetime]
DEFAULT_WEBHOOK_PROCESSING_LEASE = timedelta(minutes=5)


class PlatformWebhookAcceptanceService:
    def __init__(
        self,
        *,
        verifier: WebhookSignatureVerifier,
        payload_store: EncryptedWebhookPayloadStore,
        session_factory: SessionFactory = AsyncSessionLocal,
    ):
        self._verifier = verifier
        self._payload_store = payload_store
        self._session_factory = session_factory

    async def accept(self, envelope) -> WebhookAcceptanceResult:
        verified = await self._verifier.verify(envelope)
        payload_sha256 = compute_webhook_payload_sha256(envelope.raw_body)

        existing = await self._get_existing(
            provider_code=verified.provider_code,
            provider_event_id=verified.provider_event_id,
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha256:
                raise WebhookDuplicateConflict("Webhook event id was reused with a different payload hash")
            return WebhookAcceptanceResult(
                inbox=existing,
                accepted=False,
                duplicate_replay=True,
            )

        stored = await self._payload_store.put_verified_payload(
            provider_code=verified.provider_code,
            provider_event_id=verified.provider_event_id,
            payload_sha256=payload_sha256,
            raw_body=envelope.raw_body,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = PlatformWebhookInboxRepository(session)
                    inbox = await repository.accept(
                        verified=verified,
                        payload_sha256=payload_sha256,
                        encrypted_payload_ref=stored.encrypted_payload_ref,
                    )
        except SQLAlchemyError as exc:
            await self._payload_store.delete_uncommitted_payload(stored.encrypted_payload_ref)
            raise WebhookInboxAcceptanceFailure("Verified webhook could not be durably accepted") from exc
        except WebhookDuplicateConflict:
            await self._payload_store.delete_uncommitted_payload(stored.encrypted_payload_ref)
            raise

        return WebhookAcceptanceResult(
            inbox=inbox,
            accepted=inbox.was_created,
            duplicate_replay=inbox.duplicate_replay,
        )

    async def _get_existing(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
    ) -> WebhookInboxSnapshot | None:
        async with self._session_factory() as session:
            repository = PlatformWebhookInboxRepository(session)
            return await repository.get_by_provider_event(
                provider_code=provider_code,
                provider_event_id=provider_event_id,
            )


class PlatformWebhookProcessingService:
    def __init__(
        self,
        *,
        payload_store: EncryptedWebhookPayloadStore,
        session_factory: SessionFactory = AsyncSessionLocal,
        clock: Clock | None = None,
        processing_lease: timedelta = DEFAULT_WEBHOOK_PROCESSING_LEASE,
    ):
        if processing_lease.total_seconds() <= 0:
            raise ValueError("Webhook processing lease must be positive")
        self._payload_store = payload_store
        self._session_factory = session_factory
        self._clock = clock or _utc_now
        self._processing_lease = processing_lease

    async def process(self, inbox_id: uuid.UUID) -> WebhookProcessingResult:
        claim = await self.claim_for_processing(inbox_id)
        if not claim.claimed:
            return WebhookProcessingResult(
                inbox=claim.inbox,
                status=claim.inbox.processing_status,
                error_classification=claim.inbox.error_classification,
            )

        return await self.process_claim(claim)

    async def process_claim(self, claim: WebhookProcessingClaim) -> WebhookProcessingResult:
        try:
            raw_body = await self._payload_store.get_verified_payload(claim.inbox.encrypted_payload_ref)
            event = _normalize_stored_payload(claim.inbox, raw_body)
            return await self._apply_event(event, claim)
        except WebhookUnsupportedEvent:
            return await self._mark_ignored(claim, "unsupported_event")
        except WebhookClaimLost:
            raise
        except Exception as exc:
            return await self._mark_failed_retryable(claim, "processing_failure", exc.__class__.__name__)

    async def claim_for_processing(self, inbox_id: uuid.UUID) -> WebhookProcessingClaim:
        now = self._clock()
        stale_before = now - self._processing_lease
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformWebhookInboxRepository(session)
                inbox = await repository.claim_for_processing(
                    inbox_id,
                    now=now,
                    stale_before=stale_before,
                )
                return WebhookProcessingClaim(
                    inbox=inbox,
                    attempt_number=inbox.attempt_count,
                    claimed_at=inbox.updated_at if inbox.processing_claimed else now,
                    claimed=inbox.processing_claimed,
                )

    async def _apply_event(
        self,
        event: NormalizedWebhookEvent,
        claim: WebhookProcessingClaim,
    ) -> WebhookProcessingResult:
        if event.normalized_event_type not in {
            "provider_operation.succeeded",
            "provider_operation.failed",
            "provider_operation.unknown",
        }:
            return await self._mark_ignored(claim, "unsupported_event")
        if not event.external_operation_ref or not event.external_customer_ref:
            return await self._mark_ignored(claim, "unknown_mapping")

        async with self._session_factory() as session:
            async with session.begin():
                operation_repository = PlatformProviderOperationRepository(session)
                if event.organization_id_hint is not None:
                    await operation_repository.set_tenant_context(event.organization_id_hint)
                customer = await session.scalar(
                    select(PlatformProviderCustomer).where(
                        PlatformProviderCustomer.provider_code == event.provider_code,
                        PlatformProviderCustomer.external_customer_ref == event.external_customer_ref,
                        PlatformProviderCustomer.status == "active",
                    )
                )
                inbox_repository = PlatformWebhookInboxRepository(session)
                await inbox_repository.assert_claim_owned(
                    claim.inbox_id,
                    expected_attempt_count=claim.attempt_number,
                )
                if customer is None:
                    marked = await inbox_repository.mark_ignored(
                        claim.inbox_id,
                        expected_attempt_count=claim.attempt_number,
                        error_classification="unknown_mapping",
                        now=self._clock(),
                    )
                    return WebhookProcessingResult(marked, "ignored", error_classification="unknown_mapping")
                if event.organization_id_hint is not None and event.organization_id_hint != customer.organization_id:
                    marked = await inbox_repository.mark_ignored(
                        claim.inbox_id,
                        expected_attempt_count=claim.attempt_number,
                        error_classification="unknown_mapping",
                        error_detail_safe="tenant_mismatch",
                        now=self._clock(),
                    )
                    return WebhookProcessingResult(marked, "ignored", error_classification="unknown_mapping")

                await operation_repository.set_tenant_context(customer.organization_id)
                operation = await operation_repository.get_by_external_operation_ref(
                    provider_code=event.provider_code,
                    external_operation_ref=event.external_operation_ref,
                    for_update=True,
                )
                if operation is None:
                    marked = await inbox_repository.mark_ignored(
                        claim.inbox_id,
                        expected_attempt_count=claim.attempt_number,
                        error_classification="unknown_mapping",
                        now=self._clock(),
                    )
                    return WebhookProcessingResult(marked, "ignored", error_classification="unknown_mapping")
                if operation.organization_id != customer.organization_id:
                    marked = await inbox_repository.mark_ignored(
                        claim.inbox_id,
                        expected_attempt_count=claim.attempt_number,
                        error_classification="unknown_mapping",
                        error_detail_safe="tenant_mismatch",
                        now=self._clock(),
                    )
                    return WebhookProcessingResult(marked, "ignored", error_classification="unknown_mapping")

                await operation_repository.set_tenant_context(operation.organization_id)
                result = _operation_result_from_event(event, operation.id)
                try:
                    await operation_repository.record_result(result)
                except Exception:
                    marked = await inbox_repository.mark_failed_final(
                        claim.inbox_id,
                        expected_attempt_count=claim.attempt_number,
                        error_classification="evidence_conflict",
                        now=self._clock(),
                    )
                    return WebhookProcessingResult(
                        marked,
                        "failed_final",
                        provider_operation_id=operation.id,
                        error_classification="evidence_conflict",
                    )
                marked = await inbox_repository.mark_processed(
                    claim.inbox_id,
                    expected_attempt_count=claim.attempt_number,
                    now=self._clock(),
                )
                return WebhookProcessingResult(
                    marked,
                    "processed",
                    provider_operation_id=operation.id,
                )

    async def _mark_ignored(
        self,
        claim: WebhookProcessingClaim,
        error_classification: str,
    ) -> WebhookProcessingResult:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformWebhookInboxRepository(session)
                marked = await repository.mark_ignored(
                    claim.inbox_id,
                    expected_attempt_count=claim.attempt_number,
                    error_classification=error_classification,
                    now=self._clock(),
                )
                return WebhookProcessingResult(marked, "ignored", error_classification=error_classification)

    async def _mark_failed_retryable(
        self,
        claim: WebhookProcessingClaim,
        error_classification: str,
        error_detail_safe: str | None,
    ) -> WebhookProcessingResult:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformWebhookInboxRepository(session)
                marked = await repository.mark_failed_retryable(
                    claim.inbox_id,
                    expected_attempt_count=claim.attempt_number,
                    error_classification=error_classification,
                    error_detail_safe=error_detail_safe,
                    now=self._clock(),
                )
                return WebhookProcessingResult(
                    marked,
                    "failed_retryable",
                    error_classification=error_classification,
                )


def _normalize_stored_payload(
    inbox: WebhookInboxSnapshot,
    raw_body: bytes,
) -> NormalizedWebhookEvent:
    if compute_webhook_payload_sha256(raw_body) != inbox.payload_sha256:
        raise WebhookUnsupportedEvent("Stored webhook payload hash does not match inbox evidence")
    payload = json.loads(raw_body.decode("utf-8"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if payload.get("type") != inbox.normalized_event_type:
        raise WebhookUnsupportedEvent("Stored webhook type does not match inbox metadata")
    organization_id_hint = None
    if isinstance(data.get("organization_id"), str):
        try:
            organization_id_hint = uuid.UUID(data["organization_id"])
        except ValueError:
            organization_id_hint = None
    return NormalizedWebhookEvent(
        provider_code=inbox.provider_code,
        provider_event_id=inbox.provider_event_id,
        normalized_event_type=inbox.normalized_event_type,
        payload_sha256=inbox.payload_sha256,
        encrypted_payload_ref=inbox.encrypted_payload_ref,
        external_customer_ref=_optional_string(data.get("external_customer_ref")),
        external_operation_ref=_optional_string(data.get("external_operation_ref")),
        external_object_ref=_optional_string(data.get("external_object_ref")),
        organization_id_hint=organization_id_hint,
    )


def _operation_result_from_event(
    event: NormalizedWebhookEvent,
    operation_id: uuid.UUID,
) -> ProviderOperationResult:
    if event.normalized_event_type == "provider_operation.succeeded":
        outcome = ProviderOutcomeKind.SUCCESS
        error_classification = None
    elif event.normalized_event_type == "provider_operation.failed":
        outcome = ProviderOutcomeKind.BUSINESS_FAILURE
        error_classification = "provider_webhook_failed"
    elif event.normalized_event_type == "provider_operation.unknown":
        outcome = ProviderOutcomeKind.UNKNOWN
        error_classification = "provider_webhook_unknown"
    else:
        raise WebhookUnsupportedEvent("Unsupported webhook event type")
    return result_for_outcome(
        operation_id,
        ProviderCallResult(
            outcome=outcome,
            external_operation_ref=event.external_operation_ref,
            error_classification=error_classification,
            result_reference=f"webhook:{event.provider_code}:{event.provider_event_id}",
            result_evidence_sha256=event.payload_sha256,
        ),
    )


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
