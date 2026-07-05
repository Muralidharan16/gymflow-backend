from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.webhooks import (
    WebhookDuplicateConflict,
    WebhookEnvelope,
    WebhookPayloadStorageFailure,
    WebhookSignatureInvalid,
    WebhookSignatureMissing,
    WebhookTimestampInvalid,
    WebhookTransportHeaders,
    WebhookClaimLost,
    compute_webhook_payload_sha256,
)
from app.platform_billing.services.webhooks import (
    PlatformWebhookAcceptanceService,
    PlatformWebhookProcessingService,
)
import app.platform_billing.services.webhooks as webhook_services
from app.platform_billing.webhooks.fake import (
    DeterministicFakeWebhookVerifier,
    sign_fake_webhook,
)
from app.platform_billing.webhooks.payload_store import InMemoryEncryptedWebhookPayloadStore
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    ORG_2,
    cleanup_phase1_tables,
    seed_organizations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
TIMESTAMP = int(NOW.timestamp())
SHA_C = "c" * 64
LEASE = timedelta(minutes=5)


class MutableClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def webhook_body(
    *,
    event_id: str = "evt_phase4c",
    event_type: str = "provider_operation.succeeded",
    external_operation_ref: str = "fake_op_phase4c",
    organization_id: str | None = ORG_1,
) -> bytes:
    data = {
        "external_operation_ref": external_operation_ref,
        "external_customer_ref": "fake_customer_phase4c",
        "external_object_ref": "fake_object_phase4c",
    }
    if organization_id is not None:
        data["organization_id"] = organization_id
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "created": TIMESTAMP,
            "data": data,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def envelope(
    raw_body: bytes,
    *,
    provider_code: str = "fake",
    timestamp: int = TIMESTAMP,
    signature: str | None = None,
    include_signature: bool = True,
    include_timestamp: bool = True,
) -> WebhookEnvelope:
    headers: dict[str, str] = {}
    if include_timestamp:
        headers["x-fake-timestamp"] = str(timestamp)
    if include_signature:
        headers["x-fake-signature"] = signature or sign_fake_webhook(raw_body=raw_body, timestamp=timestamp)
    return WebhookEnvelope(
        provider_code=provider_code,
        raw_body=raw_body,
        headers=WebhookTransportHeaders(headers),
    )


def acceptance_service(store: InMemoryEncryptedWebhookPayloadStore):
    return PlatformWebhookAcceptanceService(
        verifier=DeterministicFakeWebhookVerifier(now=NOW),
        payload_store=store,
    )


async def fetch_inbox(provider_event_id: str, provider_code: str = "fake"):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT id, provider_code, provider_event_id, payload_sha256,
                       encrypted_payload_ref, normalized_event_type,
                       processing_status, attempt_count, error_classification,
                       error_detail_safe, updated_at
                FROM platform_webhook_inbox
                WHERE provider_code = :provider_code
                  AND provider_event_id = :event_id
                """
            ),
            {"provider_code": provider_code, "event_id": provider_event_id},
        )
        return result.mappings().one_or_none()


async def inbox_count() -> int:
    async with AsyncSessionLocal() as session:
        return await session.scalar(text("SELECT count(*) FROM platform_webhook_inbox"))


async def seed_provider_operation(
    *,
    status: str = "in_progress",
    external_operation_ref: str = "fake_op_phase4c",
    organization_id: str = ORG_1,
    provider_code: str = "fake",
    error_classification: str | None = None,
) -> uuid.UUID:
    await seed_provider_customer(organization_id=organization_id, provider_code=provider_code)
    operation_id = uuid.uuid4()
    completed_sql = "clock_timestamp()" if status in {"succeeded", "failed", "unknown"} else "NULL"
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        await session.execute(
            text(
                f"""
                INSERT INTO platform_provider_operations (
                    id, organization_id, provider_code, operation_type,
                    idempotency_key, canonical_request_sha256, status,
                    external_operation_ref, attempt_count, result_evidence_sha256,
                    result_reference, error_classification, completed_at
                )
                VALUES (
                    :id, :org_id, :provider_code, 'checkout_session_reserve',
                    :idempotency_key, :request_hash, :status,
                    :external_ref, 1, :result_hash, :result_reference,
                    :error_classification, {completed_sql}
                )
                """
            ),
            {
                "id": operation_id,
                "org_id": organization_id,
                "provider_code": provider_code,
                "idempotency_key": f"webhook-{operation_id}",
                "request_hash": SHA_C,
                "status": status,
                "external_ref": external_operation_ref,
                "result_hash": SHA_C if status in {"succeeded", "failed", "unknown"} else None,
                "result_reference": f"seed:{external_operation_ref}" if status in {"succeeded", "failed", "unknown"} else None,
                "error_classification": error_classification,
            },
        )
        await session.commit()
    return operation_id


async def seed_provider_customer(
    *,
    organization_id: str = ORG_1,
    provider_code: str = "fake",
    external_customer_ref: str = "fake_customer_phase4c",
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO platform_provider_customers (
                    organization_id, provider_code, external_customer_ref, status
                )
                VALUES (:org_id, :provider_code, :external_customer_ref, 'active')
                ON CONFLICT (provider_code, external_customer_ref) DO NOTHING
                """
            ),
            {
                "org_id": organization_id,
                "provider_code": provider_code,
                "external_customer_ref": external_customer_ref,
            },
        )
        await session.commit()


async def fetch_operation(operation_id: uuid.UUID, organization_id: str = ORG_1):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        result = await session.execute(
            text(
                """
                SELECT status, external_operation_ref, result_evidence_sha256,
                       result_reference, error_classification, completed_at
                FROM platform_provider_operations
                WHERE id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        )
        return result.mappings().one()


@pytest.mark.asyncio
async def test_valid_signature_accepts_hash_pointer_without_raw_body():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    raw_body = webhook_body()

    result = await acceptance_service(store).accept(envelope(raw_body))

    assert result.accepted is True
    assert result.duplicate_replay is False
    assert result.inbox.payload_sha256 == compute_webhook_payload_sha256(raw_body)
    assert result.inbox.encrypted_payload_ref.startswith("mem-encrypted://fake/evt_phase4c/")
    assert store.put_calls == [result.inbox.encrypted_payload_ref]
    row = await fetch_inbox("evt_phase4c")
    assert row is not None
    assert row["payload_sha256"] == compute_webhook_payload_sha256(raw_body)
    assert raw_body.decode("utf-8") not in str(dict(row))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_error", "envelope_kwargs", "body_mutation"),
    [
        (WebhookSignatureInvalid, {"signature": "v1=bad"}, None),
        (WebhookSignatureMissing, {"include_signature": False}, None),
        (WebhookSignatureInvalid, {"signature": "not-versioned"}, None),
        (WebhookSignatureInvalid, {}, b'{"altered":true}'),
        (WebhookTimestampInvalid, {"timestamp": TIMESTAMP - 999_999}, None),
        (WebhookSignatureMissing, {"include_timestamp": False}, None),
    ],
)
async def test_invalid_signature_variants_create_no_inbox_or_payload(expected_error, envelope_kwargs, body_mutation):
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    signed_body = webhook_body(event_id="evt_invalid")
    received_body = body_mutation or signed_body

    with pytest.raises(expected_error):
        await acceptance_service(store).accept(envelope(received_body, **envelope_kwargs))

    assert await inbox_count() == 0
    assert store.put_calls == []
    assert store.payloads == {}


@pytest.mark.asyncio
async def test_altered_body_with_original_signature_is_rejected_before_storage():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    original = webhook_body(event_id="evt_altered_exact", external_operation_ref="fake_op_original")
    altered = webhook_body(event_id="evt_altered_exact", external_operation_ref="fake_op_altered")
    original_signature = sign_fake_webhook(raw_body=original, timestamp=TIMESTAMP)

    with pytest.raises(WebhookSignatureInvalid):
        await acceptance_service(store).accept(
            envelope(altered, signature=original_signature)
        )

    assert await inbox_count() == 0
    assert store.put_calls == []


@pytest.mark.asyncio
async def test_untrusted_identity_headers_cannot_override_signed_payload_identity():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    raw_body = webhook_body(event_id="evt_signed_identity", event_type="provider_operation.succeeded")
    signed = envelope(raw_body)
    signed.headers.values["x-fake-event-id"] = "evt_unsigned_override"
    signed.headers.values["x-fake-event-type"] = "provider_operation.failed"

    result = await acceptance_service(store).accept(signed)

    assert result.inbox.provider_event_id == "evt_signed_identity"
    assert result.inbox.normalized_event_type == "provider_operation.succeeded"
    assert await fetch_inbox("evt_unsigned_override") is None


@pytest.mark.asyncio
async def test_future_timestamp_is_rejected_before_storage():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    raw_body = webhook_body(event_id="evt_future")
    future_timestamp = TIMESTAMP + 999_999

    with pytest.raises(WebhookTimestampInvalid):
        await acceptance_service(store).accept(
            envelope(raw_body, timestamp=future_timestamp)
        )

    assert await inbox_count() == 0
    assert store.put_calls == []


@pytest.mark.asyncio
async def test_payload_storage_failure_creates_no_inbox_row():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore(fail_put=True)

    with pytest.raises(WebhookPayloadStorageFailure):
        await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_store_failure")))

    assert await inbox_count() == 0


@pytest.mark.asyncio
async def test_database_failure_after_payload_storage_deletes_uncommitted_payload(monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    raw_body = webhook_body(event_id="evt_db_failure")

    async def fail_accept(*args, **kwargs):
        raise SQLAlchemyError("forced insert failure")

    monkeypatch.setattr(webhook_services.PlatformWebhookInboxRepository, "accept", fail_accept)

    with pytest.raises(webhook_services.WebhookInboxAcceptanceFailure):
        await acceptance_service(store).accept(envelope(raw_body))

    assert await inbox_count() == 0
    assert len(store.put_calls) == 1
    assert store.delete_calls == store.put_calls
    assert store.payloads == {}


@pytest.mark.asyncio
async def test_duplicate_same_event_same_hash_replays_existing_without_second_row_or_store():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    service = acceptance_service(store)
    raw_body = webhook_body(event_id="evt_duplicate")

    first = await service.accept(envelope(raw_body))
    second = await service.accept(envelope(raw_body))

    assert first.inbox.id == second.inbox.id
    assert second.accepted is False
    assert second.duplicate_replay is True
    assert await inbox_count() == 1
    assert len(store.put_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_same_event_different_hash_conflicts_and_preserves_original():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    service = acceptance_service(store)
    original = webhook_body(event_id="evt_conflict", external_operation_ref="fake_op_original")
    altered = webhook_body(event_id="evt_conflict", external_operation_ref="fake_op_altered")

    first = await service.accept(envelope(original))
    with pytest.raises(WebhookDuplicateConflict):
        await service.accept(envelope(altered))

    row = await fetch_inbox("evt_conflict")
    assert row["payload_sha256"] == first.inbox.payload_sha256
    assert row["encrypted_payload_ref"] == first.inbox.encrypted_payload_ref
    assert await inbox_count() == 1


@pytest.mark.asyncio
async def test_same_payload_different_event_id_and_different_provider_same_event_are_independent():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    service = acceptance_service(store)
    raw_one = webhook_body(event_id="evt_independent_one")
    raw_two = webhook_body(event_id="evt_independent_two")

    one = await service.accept(envelope(raw_one))
    two = await service.accept(envelope(raw_two))
    other_provider = await service.accept(envelope(raw_one, provider_code="another_fake"))

    assert one.inbox.id != two.inbox.id
    assert one.inbox.id != other_provider.inbox.id
    assert await inbox_count() == 3


@pytest.mark.asyncio
async def test_concurrent_duplicate_acceptance_creates_one_inbox_row():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    service = acceptance_service(store)
    raw_body = webhook_body(event_id="evt_concurrent_accept")

    results = await asyncio.gather(
        service.accept(envelope(raw_body)),
        service.accept(envelope(raw_body)),
    )

    assert {result.inbox.id for result in results} == {results[0].inbox.id}
    assert await inbox_count() == 1


@pytest.mark.asyncio
async def test_processing_claim_runs_once_and_resolves_in_progress_operation():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_process_once")))
    processor = PlatformWebhookProcessingService(payload_store=store)

    results = await asyncio.gather(
        processor.process(accepted.inbox.id),
        processor.process(accepted.inbox.id),
    )

    assert sorted(result.status for result in results) == ["processed", "processing"]
    assert len(store.get_calls) == 1
    inbox = await fetch_inbox("evt_process_once")
    assert inbox["processing_status"] == "processed"
    assert inbox["attempt_count"] == 1
    operation = await fetch_operation(operation_id)
    assert operation["status"] == "succeeded"
    assert operation["result_evidence_sha256"] == accepted.inbox.payload_sha256


@pytest.mark.asyncio
async def test_processing_lease_claim_matrix_and_exact_cutoff_boundary():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    clock = MutableClock()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_lease_matrix")))
    processor = PlatformWebhookProcessingService(payload_store=store, clock=clock, processing_lease=LEASE)

    first = await processor.claim_for_processing(accepted.inbox.id)
    clock.now = first.inbox.updated_at
    second = await processor.claim_for_processing(accepted.inbox.id)
    clock.advance(LEASE)
    boundary = await processor.claim_for_processing(accepted.inbox.id)
    clock.advance(timedelta(microseconds=1))
    reclaimed = await processor.claim_for_processing(accepted.inbox.id)

    assert first.claimed is True
    assert first.attempt_number == 1
    assert first.claimed_at == first.inbox.updated_at
    assert second.claimed is False
    assert second.inbox.processing_status == "processing"
    assert second.attempt_number == 1
    assert boundary.claimed is False
    assert boundary.attempt_number == 1
    assert reclaimed.claimed is True
    assert reclaimed.attempt_number == 2

    await processor.process_claim(reclaimed)
    terminal = await processor.claim_for_processing(accepted.inbox.id)
    assert terminal.claimed is False
    assert terminal.inbox.processing_status == "processed"


@pytest.mark.asyncio
async def test_failed_retryable_reclaims_immediately_and_processed_replay_does_not_increment_attempts():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_retry_claim")))
    failing_store = InMemoryEncryptedWebhookPayloadStore(fail_get=True, payloads=dict(store.payloads))

    failed = await PlatformWebhookProcessingService(payload_store=failing_store).process(accepted.inbox.id)
    retry_claim = await PlatformWebhookProcessingService(payload_store=store).claim_for_processing(accepted.inbox.id)

    assert failed.status == "failed_retryable"
    assert retry_claim.claimed is True
    assert retry_claim.attempt_number == 2
    assert (await fetch_inbox("evt_retry_claim"))["attempt_count"] == 2

    processor = PlatformWebhookProcessingService(payload_store=store)
    processed = await processor.process_claim(retry_claim)
    replay = await processor.process(accepted.inbox.id)

    assert processed.status == "processed"
    assert replay.status == "processed"
    assert (await fetch_inbox("evt_retry_claim"))["attempt_count"] == 2


@pytest.mark.asyncio
async def test_stale_worker_cannot_finalize_after_reclaim_and_winning_worker_can_finish():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_stale_success")))
    clock = MutableClock()
    processor = PlatformWebhookProcessingService(payload_store=store, clock=clock, processing_lease=LEASE)

    worker_a = await processor.claim_for_processing(accepted.inbox.id)
    clock.now = worker_a.inbox.updated_at
    clock.advance(LEASE + timedelta(microseconds=1))
    worker_b = await processor.claim_for_processing(accepted.inbox.id)

    with pytest.raises(WebhookClaimLost):
        await processor.process_claim(worker_a)

    inbox_after_lost = await fetch_inbox("evt_stale_success")
    assert inbox_after_lost["processing_status"] == "processing"
    assert inbox_after_lost["attempt_count"] == 2
    assert (await fetch_operation(operation_id))["status"] == "in_progress"

    final = await processor.process_claim(worker_b)
    assert final.status == "processed"
    assert (await fetch_operation(operation_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_stale_worker_cannot_mark_failed_retryable_after_reclaim():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_stale_retry")))
    clock = MutableClock()
    processor = PlatformWebhookProcessingService(payload_store=store, clock=clock, processing_lease=LEASE)
    worker_a = await processor.claim_for_processing(accepted.inbox.id)
    clock.now = worker_a.inbox.updated_at
    clock.advance(LEASE + timedelta(microseconds=1))
    worker_b = await processor.claim_for_processing(accepted.inbox.id)
    failing_store = InMemoryEncryptedWebhookPayloadStore(fail_get=True, payloads=dict(store.payloads))
    stale_processor = PlatformWebhookProcessingService(payload_store=failing_store, clock=clock, processing_lease=LEASE)

    with pytest.raises(WebhookClaimLost):
        await stale_processor.process_claim(worker_a)

    inbox_after_lost = await fetch_inbox("evt_stale_retry")
    assert inbox_after_lost["processing_status"] == "processing"
    assert inbox_after_lost["attempt_count"] == 2
    assert await processor.process_claim(worker_b)


@pytest.mark.asyncio
async def test_stale_worker_cannot_mark_failed_final_after_reclaim():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="succeeded", external_operation_ref="fake_op_stale_conflict")
    before = await fetch_operation(operation_id)
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id="evt_stale_final",
                event_type="provider_operation.failed",
                external_operation_ref="fake_op_stale_conflict",
            )
        )
    )
    clock = MutableClock()
    processor = PlatformWebhookProcessingService(payload_store=store, clock=clock, processing_lease=LEASE)
    worker_a = await processor.claim_for_processing(accepted.inbox.id)
    clock.now = worker_a.inbox.updated_at
    clock.advance(LEASE + timedelta(microseconds=1))
    worker_b = await processor.claim_for_processing(accepted.inbox.id)

    with pytest.raises(WebhookClaimLost):
        await processor.process_claim(worker_a)

    after_lost = await fetch_operation(operation_id)
    assert after_lost["status"] == before["status"]
    assert after_lost["result_evidence_sha256"] == before["result_evidence_sha256"]
    assert (await fetch_inbox("evt_stale_final"))["attempt_count"] == 2

    final = await processor.process_claim(worker_b)
    assert final.status == "failed_final"


@pytest.mark.asyncio
async def test_finalization_failure_rolls_back_provider_operation_transition(monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_atomic_rollback")))

    async def fail_mark_processed(*args, **kwargs):
        raise SQLAlchemyError("forced finalization failure")

    monkeypatch.setattr(webhook_services.PlatformWebhookInboxRepository, "mark_processed", fail_mark_processed)

    result = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert result.status == "failed_retryable"
    assert (await fetch_operation(operation_id))["status"] == "in_progress"
    inbox = await fetch_inbox("evt_atomic_rollback")
    assert inbox["processing_status"] == "failed_retryable"
    assert inbox["attempt_count"] == 1


@pytest.mark.asyncio
async def test_two_workers_reclaiming_stale_event_yield_one_winner():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_reclaim_race")))
    clock = MutableClock()
    processor = PlatformWebhookProcessingService(payload_store=store, clock=clock, processing_lease=LEASE)

    await processor.claim_for_processing(accepted.inbox.id)
    current = await fetch_inbox("evt_reclaim_race")
    clock.now = current["updated_at"]
    clock.advance(LEASE + timedelta(microseconds=1))
    claims = await asyncio.gather(
        processor.claim_for_processing(accepted.inbox.id),
        processor.claim_for_processing(accepted.inbox.id),
    )

    assert sum(1 for claim in claims if claim.claimed) == 1
    winner = next(claim for claim in claims if claim.claimed)
    loser = next(claim for claim in claims if not claim.claimed)
    assert winner.attempt_number == 2
    assert loser.attempt_number == 2
    assert (await fetch_inbox("evt_reclaim_race"))["attempt_count"] == 2
    assert (await processor.process_claim(winner)).status == "processed"


@pytest.mark.asyncio
async def test_payload_store_read_happens_without_inbox_row_lock_or_active_service_transaction():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_lock_probe")))
    sessions = []
    lock_probe_results: list[bool] = []

    def tracking_session_factory():
        session = AsyncSessionLocal()
        sessions.append(session)
        return session

    class ProbingStore(InMemoryEncryptedWebhookPayloadStore):
        async def get_verified_payload(self, encrypted_payload_ref: str) -> bytes:
            async with AsyncSessionLocal() as probe_session:
                try:
                    await probe_session.execute(
                        text(
                            """
                            SELECT id
                            FROM platform_webhook_inbox
                            WHERE id = :inbox_id
                            FOR UPDATE NOWAIT
                            """
                        ),
                        {"inbox_id": accepted.inbox.id},
                    )
                    lock_probe_results.append(True)
                except Exception:
                    lock_probe_results.append(False)
                finally:
                    await probe_session.rollback()
            assert all(not session.in_transaction() for session in sessions)
            return await super().get_verified_payload(encrypted_payload_ref)

    probing_store = ProbingStore(payloads=dict(store.payloads))
    processor = PlatformWebhookProcessingService(
        payload_store=probing_store,
        session_factory=tracking_session_factory,
    )

    result = await processor.process(accepted.inbox.id)

    assert result.status == "processed"
    assert lock_probe_results == [True]
    assert all(not session.in_transaction() for session in sessions)


@pytest.mark.asyncio
async def test_processing_storage_failure_records_retryable_state_then_recovers():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_retryable")))
    failing_store = InMemoryEncryptedWebhookPayloadStore(fail_get=True, payloads=dict(store.payloads))

    failed = await PlatformWebhookProcessingService(payload_store=failing_store).process(accepted.inbox.id)
    inbox_after_failure = await fetch_inbox("evt_retryable")
    recovered = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert failed.status == "failed_retryable"
    assert inbox_after_failure["processing_status"] == "failed_retryable"
    assert inbox_after_failure["attempt_count"] == 1
    assert recovered.status == "processed"
    assert (await fetch_inbox("evt_retryable"))["attempt_count"] == 2
    assert (await fetch_operation(operation_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_processed_event_is_not_processed_twice():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_provider_operation(status="in_progress")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(envelope(webhook_body(event_id="evt_processed_replay")))
    processor = PlatformWebhookProcessingService(payload_store=store)

    first = await processor.process(accepted.inbox.id)
    second = await processor.process(accepted.inbox.id)

    assert first.status == "processed"
    assert second.status == "processed"
    assert len(store.get_calls) == 1


@pytest.mark.asyncio
async def test_unsupported_event_and_unknown_mapping_are_recorded_safely():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    unsupported = await acceptance_service(store).accept(
        envelope(webhook_body(event_id="evt_unsupported", event_type="customer.updated"))
    )
    missing = await acceptance_service(store).accept(
        envelope(webhook_body(event_id="evt_missing", external_operation_ref="fake_op_missing"))
    )
    processor = PlatformWebhookProcessingService(payload_store=store)

    unsupported_result = await processor.process(unsupported.inbox.id)
    missing_result = await processor.process(missing.inbox.id)

    assert unsupported_result.status == "ignored"
    assert unsupported_result.error_classification == "unsupported_event"
    assert missing_result.status == "ignored"
    assert missing_result.error_classification == "unknown_mapping"


@pytest.mark.asyncio
async def test_cross_tenant_mapping_is_denied_without_guessing_tenant():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress", organization_id=ORG_1)
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(
        envelope(webhook_body(event_id="evt_cross_tenant", organization_id=ORG_2))
    )

    result = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert result.status == "ignored"
    assert result.error_classification == "unknown_mapping"
    assert (await fetch_operation(operation_id))["status"] == "in_progress"


@pytest.mark.asyncio
async def test_unknown_tenant_hint_without_local_customer_mapping_is_unknown_mapping():
    await cleanup_phase1_tables()
    await seed_organizations()
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(
        envelope(webhook_body(event_id="evt_no_local_customer", organization_id=ORG_2))
    )

    result = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert result.status == "ignored"
    assert result.error_classification == "unknown_mapping"


@pytest.mark.asyncio
async def test_same_external_reference_under_another_provider_does_not_cross_match():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status="in_progress", provider_code="fake")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(
        envelope(
            webhook_body(event_id="evt_cross_provider", external_operation_ref="fake_op_phase4c"),
            provider_code="another_fake",
        )
    )

    result = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert result.status == "ignored"
    assert result.error_classification == "unknown_mapping"
    assert (await fetch_operation(operation_id))["status"] == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "event_type", "expected_status"),
    [
        ("in_progress", "provider_operation.succeeded", "succeeded"),
        ("unknown", "provider_operation.succeeded", "succeeded"),
        ("in_progress", "provider_operation.failed", "failed"),
        ("unknown", "provider_operation.failed", "failed"),
    ],
)
async def test_verified_operation_evidence_resolves_allowed_states(initial_status, event_type, expected_status):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(status=initial_status, external_operation_ref=f"fake_op_{event_type}")
    store = InMemoryEncryptedWebhookPayloadStore()
    accepted = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id=f"evt_{initial_status}_{expected_status}",
                event_type=event_type,
                external_operation_ref=f"fake_op_{event_type}",
            )
        )
    )

    result = await PlatformWebhookProcessingService(payload_store=store).process(accepted.inbox.id)

    assert result.status == "processed"
    assert (await fetch_operation(operation_id))["status"] == expected_status


@pytest.mark.asyncio
async def test_repeated_terminal_evidence_is_idempotent_and_conflicting_terminal_evidence_is_rejected():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(
        status="succeeded",
        external_operation_ref="fake_op_terminal",
    )
    store = InMemoryEncryptedWebhookPayloadStore()
    success_event = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id="evt_terminal_success",
                event_type="provider_operation.succeeded",
                external_operation_ref="fake_op_terminal",
            )
        )
    )
    failure_event = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id="evt_terminal_failure",
                event_type="provider_operation.failed",
                external_operation_ref="fake_op_terminal",
            )
        )
    )
    processor = PlatformWebhookProcessingService(payload_store=store)

    repeated = await processor.process(success_event.inbox.id)
    conflicting = await processor.process(failure_event.inbox.id)

    assert repeated.status == "processed"
    assert conflicting.status == "failed_final"
    assert conflicting.error_classification == "evidence_conflict"
    assert (await fetch_operation(operation_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_repeated_failed_evidence_is_idempotent_and_failed_to_success_conflict_is_rejected():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_provider_operation(
        status="failed",
        external_operation_ref="fake_op_failed_terminal",
        error_classification="provider_declined",
    )
    before = await fetch_operation(operation_id)
    store = InMemoryEncryptedWebhookPayloadStore()
    failure_event = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id="evt_terminal_failed_repeat",
                event_type="provider_operation.failed",
                external_operation_ref="fake_op_failed_terminal",
            )
        )
    )
    success_event = await acceptance_service(store).accept(
        envelope(
            webhook_body(
                event_id="evt_terminal_failed_conflict",
                event_type="provider_operation.succeeded",
                external_operation_ref="fake_op_failed_terminal",
            )
        )
    )
    processor = PlatformWebhookProcessingService(payload_store=store)

    repeated = await processor.process(failure_event.inbox.id)
    conflicting = await processor.process(success_event.inbox.id)
    after = await fetch_operation(operation_id)

    assert repeated.status == "processed"
    assert conflicting.status == "failed_final"
    assert conflicting.error_classification == "evidence_conflict"
    assert after["status"] == "failed"
    assert after["result_evidence_sha256"] == before["result_evidence_sha256"]
    assert after["completed_at"] == before["completed_at"]
    assert after["error_classification"] == before["error_classification"]


def test_phase4c_safety_guardrails_remain_invisible_and_non_enforcing():
    from app.core.config import settings

    assert settings.PLATFORM_BILLING_CHECKOUT is False
    assert settings.PLATFORM_BILLING_WEBHOOK_PROCESSING is False
    assert settings.PLATFORM_BILLING_DUNNING_TRANSITIONS is False
    assert settings.PLATFORM_BILLING_NOTIFICATIONS is False
    assert settings.PLATFORM_BILLING_ENFORCEMENT is False
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "completion.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "simulation.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "callback.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "webhooks.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "tasks" / "reconciliation.py").exists()

    phase4c_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            REPO_ROOT / "app" / "platform_billing" / "domain" / "webhooks.py",
            REPO_ROOT / "app" / "platform_billing" / "repositories" / "webhooks.py",
            REPO_ROOT / "app" / "platform_billing" / "services" / "webhooks.py",
            REPO_ROOT / "app" / "platform_billing" / "webhooks" / "contracts.py",
            REPO_ROOT / "app" / "platform_billing" / "webhooks" / "fake.py",
            REPO_ROOT / "app" / "platform_billing" / "webhooks" / "payload_store.py",
        ]
    )
    forbidden = ["requests.", "httpx.", "razorpay", "cashfree", "stripe", "api_key", "secret_key"]
    for token in forbidden:
        assert token not in phase4c_source

    service_source = (REPO_ROOT / "app" / "platform_billing" / "services" / "webhooks.py").read_text(encoding="utf-8")
    assert "PlatformSubscription" not in service_source
    assert "PlatformEntitlementProjection" not in service_source
    assert "PlatformAccessProjection" not in service_source
