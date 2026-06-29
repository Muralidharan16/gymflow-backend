from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.platform_billing.api.schemas import FakeCheckoutSimulationResponse
from app.platform_billing.domain.provider_operations import (
    IdempotencyConflict,
    ProviderOperationRequest,
    ProviderOperationResult,
)
from app.platform_billing.domain.webhooks import WebhookEnvelope, WebhookTransportHeaders
from app.platform_billing.models.provider import PlatformProviderCustomer, PlatformProviderOperation
from app.platform_billing.providers.fake_checkout_simulation import (
    CONFIRM_CHECKOUT_OPERATION_TYPE,
    DeterministicFakeCheckoutOutcomeProducer,
)
from app.platform_billing.repositories.provider_operations import PlatformProviderOperationRepository
from app.platform_billing.services.webhooks import PlatformWebhookAcceptanceService, PlatformWebhookProcessingService
from app.platform_billing.webhooks.fake import (
    FAKE_WEBHOOK_SIGNATURE_HEADER,
    FAKE_WEBHOOK_TIMESTAMP_HEADER,
    DeterministicFakeWebhookVerifier,
)
from app.platform_billing.webhooks.contracts import EncryptedWebhookPayloadStore
from app.platform_billing.webhooks.payload_store import LocalEncryptedWebhookPayloadStore


class CheckoutSimulationConflictError(Exception):
    pass


class CheckoutSimulationInvalidStateError(Exception):
    pass


class CheckoutSimulationNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class CheckoutSimulationServices:
    event_producer: DeterministicFakeCheckoutOutcomeProducer
    payload_store: EncryptedWebhookPayloadStore


def default_simulation_services() -> CheckoutSimulationServices:
    return CheckoutSimulationServices(
        event_producer=DeterministicFakeCheckoutOutcomeProducer(),
        payload_store=LocalEncryptedWebhookPayloadStore(Path(settings.PLATFORM_BILLING_WEBHOOK_PAYLOAD_STORE_DIR)),
    )


class PlatformCheckoutSimulationService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        simulation_services: CheckoutSimulationServices | None = None,
    ):
        self._db = db
        self._session_factory = session_factory
        services = simulation_services or default_simulation_services()
        self._event_producer = services.event_producer
        self._payload_store = services.payload_store

    async def create_simulation(
        self,
        *,
        checkout_operation_id: uuid.UUID,
        requested_outcome: str,
        organization_id: uuid.UUID,
        idempotency_key: str,
    ) -> FakeCheckoutSimulationResponse:
        if requested_outcome not in {"pending", "succeeded", "failed"}:
            raise CheckoutSimulationInvalidStateError("invalid_outcome")

        checkout = await self._load_checkout_operation(checkout_operation_id, organization_id)
        customer = await self._load_active_fake_customer(organization_id)
        external_ref = self._event_producer.external_operation_ref(
            organization_id=organization_id,
            checkout_operation_id=checkout_operation_id,
            checkout_session_reference=checkout.result_reference or "",
        )

        reserved = await self._reserve_or_reuse_confirm_operation(
            organization_id=organization_id,
            checkout_operation_id=checkout_operation_id,
            checkout_session_reference=checkout.result_reference or "",
            requested_outcome=requested_outcome,
            idempotency_key=idempotency_key,
            external_operation_ref=external_ref,
        )

        if reserved.status in {"succeeded", "failed"}:
            if _status_to_outcome(reserved.status) != requested_outcome:
                raise CheckoutSimulationConflictError("terminal_outcome_conflict")
            return _response_from_operation(
                checkout_operation_id=checkout_operation_id,
                operation=reserved,
                webhook_processing_status="processed",
                replayed=True,
            )

        if requested_outcome == "pending":
            return _response_from_operation(
                checkout_operation_id=checkout_operation_id,
                operation=reserved,
                webhook_processing_status=None,
                replayed=not reserved.was_created and not reserved.execution_claimed,
            )

        if not reserved.execution_claimed:
            refreshed = await self._wait_for_terminal(reserved.id, organization_id)
            return _response_from_operation(
                checkout_operation_id=checkout_operation_id,
                operation=refreshed,
                webhook_processing_status="processed" if refreshed.status in {"succeeded", "failed"} else None,
                replayed=True,
            )

        event = self._event_producer.generate(
            organization_id=organization_id,
            checkout_operation_id=checkout_operation_id,
            checkout_session_reference=checkout.result_reference or "",
            simulation_operation_id=reserved.id,
            external_operation_ref=external_ref,
            provider_customer_ref=customer.external_customer_ref,
            requested_outcome=requested_outcome,
        )
        envelope = WebhookEnvelope(
            provider_code="fake",
            raw_body=event.raw_body,
            headers=WebhookTransportHeaders(
                {
                    FAKE_WEBHOOK_TIMESTAMP_HEADER: str(event.event_timestamp),
                    FAKE_WEBHOOK_SIGNATURE_HEADER: event.signature,
                }
            ),
        )
        acceptance = await PlatformWebhookAcceptanceService(
            verifier=DeterministicFakeWebhookVerifier(now=datetime.fromtimestamp(event.event_timestamp, tz=timezone.utc)),
            payload_store=self._payload_store,
            session_factory=self._session_factory,
        ).accept(envelope)
        processing = await PlatformWebhookProcessingService(
            payload_store=self._payload_store,
            session_factory=self._session_factory,
        ).process(acceptance.inbox.id)
        refreshed = await self._get_confirm_operation(reserved.id, organization_id)
        if refreshed is None:
            raise CheckoutSimulationInvalidStateError("simulation_operation_missing")
        return _response_from_operation(
            checkout_operation_id=checkout_operation_id,
            operation=refreshed,
            webhook_processing_status=processing.status,
            replayed=acceptance.duplicate_replay,
        )

    async def get_simulation(
        self,
        *,
        simulation_operation_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> FakeCheckoutSimulationResponse | None:
        operation = await self._get_confirm_operation(simulation_operation_id, organization_id)
        if operation is None:
            return None
        checkout_id = _checkout_id_from_external_ref(operation.external_operation_ref)
        return FakeCheckoutSimulationResponse(
            simulation_operation_id=operation.id,
            checkout_operation_id=checkout_id or uuid.UUID(int=0),
            outcome_status=_public_status(operation.status),
            webhook_processing_status="processed" if operation.status in {"succeeded", "failed"} else None,
            provider_event_reference=operation.result_reference,
            replayed=False,
            browser_authoritative=False,
            subscription_activated=False,
        )

    async def _load_checkout_operation(self, checkout_operation_id: uuid.UUID, organization_id: uuid.UUID):
        repo = PlatformProviderOperationRepository(self._db)
        await repo.set_tenant_context(organization_id)
        checkout = await repo.get_by_id(checkout_operation_id)
        if (
            checkout is None
            or checkout.organization_id != organization_id
            or checkout.provider_code != "fake"
            or checkout.operation_type != "create_checkout"
        ):
            raise CheckoutSimulationNotFoundError("checkout_not_found")
        if checkout.status != "succeeded":
            raise CheckoutSimulationInvalidStateError("checkout_not_succeeded")
        if not checkout.result_reference:
            raise CheckoutSimulationInvalidStateError("checkout_session_missing")
        return checkout

    async def _load_active_fake_customer(self, organization_id: uuid.UUID) -> PlatformProviderCustomer:
        result = await self._db.execute(
            select(PlatformProviderCustomer).where(
                PlatformProviderCustomer.organization_id == organization_id,
                PlatformProviderCustomer.provider_code == "fake",
                PlatformProviderCustomer.status == "active",
            )
        )
        customer = result.scalar_one_or_none()
        if customer is None:
            raise CheckoutSimulationInvalidStateError("provider_customer_missing")
        return customer

    async def _reserve_or_reuse_confirm_operation(
        self,
        *,
        organization_id: uuid.UUID,
        checkout_operation_id: uuid.UUID,
        checkout_session_reference: str,
        requested_outcome: str,
        idempotency_key: str,
        external_operation_ref: str,
    ):
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_catalog.pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"platform_billing:confirm_checkout:{organization_id}:{checkout_operation_id}"},
                )
                repo = PlatformProviderOperationRepository(session)
                await repo.set_tenant_context(organization_id)
                same_key = await repo.get_by_idempotency(
                    organization_id=organization_id,
                    provider_code="fake",
                    idempotency_key=idempotency_key,
                    for_update=True,
                )
                request = ProviderOperationRequest(
                    organization_id=organization_id,
                    provider_code="fake",
                    operation_type=CONFIRM_CHECKOUT_OPERATION_TYPE,
                    idempotency_key=idempotency_key,
                    provider_customer_ref=None,
                    metadata={
                        "checkout_operation_id": str(checkout_operation_id),
                        "checkout_session_reference": checkout_session_reference,
                        "provider_mode": "fake",
                        "requested_outcome": requested_outcome,
                    },
                )
                if same_key is not None:
                    from app.platform_billing.domain.provider_operations import compute_provider_request_hash

                    if same_key.canonical_request_sha256 != compute_provider_request_hash(request):
                        raise CheckoutSimulationConflictError("idempotency_conflict")
                    return same_key

                existing = await repo.get_by_external_operation_ref(
                    provider_code="fake",
                    external_operation_ref=external_operation_ref,
                    for_update=True,
                )
                if existing is not None:
                    if existing.operation_type != CONFIRM_CHECKOUT_OPERATION_TYPE or existing.organization_id != organization_id:
                        raise CheckoutSimulationConflictError("external_reference_conflict")
                    if existing.status in {"succeeded", "failed"}:
                        if _status_to_outcome(existing.status) != requested_outcome:
                            raise CheckoutSimulationConflictError("terminal_outcome_conflict")
                        return existing
                    if requested_outcome in {"succeeded", "failed"}:
                        return await self._claim_terminal_intent(
                            session,
                            existing,
                            requested_outcome=requested_outcome,
                        )
                    return existing

                reserved = await repo.reserve(request)
                claimed = await repo.claim_for_execution(
                    reserved.id,
                    external_operation_ref=external_operation_ref,
                    result_reference=_terminal_intent_reference(requested_outcome),
                )
                return claimed

    async def _claim_terminal_intent(
        self,
        session: AsyncSession,
        existing,
        *,
        requested_outcome: str,
    ):
        intent_reference = _terminal_intent_reference(requested_outcome)
        if existing.result_reference is not None:
            if existing.result_reference == intent_reference:
                return replace(existing, execution_claimed=True)
            raise CheckoutSimulationConflictError("terminal_outcome_conflict")
        statement = (
            update(PlatformProviderOperation)
            .where(
                PlatformProviderOperation.id == existing.id,
                PlatformProviderOperation.status == "in_progress",
                PlatformProviderOperation.result_reference.is_(None),
            )
            .values(result_reference=intent_reference)
            .returning(PlatformProviderOperation)
        )
        result = await session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise CheckoutSimulationConflictError("terminal_outcome_conflict")
        await session.flush()
        snapshot = await PlatformProviderOperationRepository(session).get_by_id(row.id)
        if snapshot is None:
            raise CheckoutSimulationInvalidStateError("simulation_operation_missing")
        return replace(snapshot, execution_claimed=True)


    async def _wait_for_terminal(self, operation_id: uuid.UUID, organization_id: uuid.UUID):
        for _ in range(50):
            await asyncio.sleep(0.01)
            operation = await self._get_confirm_operation(operation_id, organization_id)
            if operation is not None and operation.status in {"succeeded", "failed", "unknown"}:
                return operation
        operation = await self._get_confirm_operation(operation_id, organization_id)
        if operation is None:
            raise CheckoutSimulationInvalidStateError("simulation_operation_missing")
        return operation

    async def _get_confirm_operation(self, operation_id: uuid.UUID, organization_id: uuid.UUID):
        async with self._session_factory() as session:
            repo = PlatformProviderOperationRepository(session)
            await repo.set_tenant_context(organization_id)
            operation = await repo.get_by_id(operation_id)
            if (
                operation is None
                or operation.organization_id != organization_id
                or operation.provider_code != "fake"
                or operation.operation_type != CONFIRM_CHECKOUT_OPERATION_TYPE
            ):
                return None
            return operation


def _terminal_intent_reference(requested_outcome: str) -> str | None:
    if requested_outcome in {"succeeded", "failed"}:
        return f"fake_confirm_intent:{requested_outcome}"
    return None


def _status_to_outcome(status: str) -> str:
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    return "pending"


def _public_status(status: str) -> str:
    if status == "succeeded":
        return "outcome_succeeded"
    if status == "failed":
        return "outcome_failed"
    if status == "unknown":
        return "outcome_unknown"
    return "outcome_pending"


def _checkout_id_from_external_ref(external_operation_ref: str | None) -> uuid.UUID | None:
    if not external_operation_ref or not external_operation_ref.startswith("fake_confirm_"):
        return None
    parts = external_operation_ref.split("_")
    if len(parts) < 3:
        return None
    try:
        return uuid.UUID(hex=parts[2])
    except ValueError:
        return None


def _response_from_operation(*, checkout_operation_id: uuid.UUID, operation, webhook_processing_status: str | None, replayed: bool):
    return FakeCheckoutSimulationResponse(
        simulation_operation_id=operation.id,
        checkout_operation_id=checkout_operation_id,
        outcome_status=_public_status(operation.status),
        webhook_processing_status=webhook_processing_status,
        provider_event_reference=operation.result_reference,
        replayed=replayed,
        browser_authoritative=False,
        subscription_activated=False,
    )
