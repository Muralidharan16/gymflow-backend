from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.provider_operations import (
    IdempotencyConflict,
    IllegalProviderOperationTransition,
    ProviderCallRequest,
    ProviderOperationRequest,
    ProviderOutcomeKind,
    ProviderResultPersistenceFailure,
    compute_provider_request_hash,
    result_for_outcome,
)
from app.platform_billing.providers.fake import DeterministicFakeProvider
from app.platform_billing.repositories.provider_operations import (
    PlatformProviderOperationRepository,
)
from app.platform_billing.services.provider_operations import (
    PlatformProviderOperationService,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    cleanup_phase1_tables,
    seed_organizations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def make_request(
    *,
    idempotency_key: str = "phase4b-key",
    amount_minor: int = 99900,
    metadata: dict[str, object] | None = None,
) -> ProviderOperationRequest:
    return ProviderOperationRequest(
        organization_id=uuid.UUID(ORG_1),
        provider_code="fake",
        operation_type="checkout_session_reserve",
        idempotency_key=idempotency_key,
        amount_minor=amount_minor,
        currency_code="INR",
        plan_version_id=uuid.UUID("83000000-0000-0000-0000-000000000201"),
        price_id=uuid.UUID("83000000-0000-0000-0000-000000000301"),
        provider_customer_ref="fake_customer_ref",
        provider_payment_method_ref="fake_payment_method_ref",
        metadata=metadata or {"purpose": "phase4b", "nested": {"b": 2, "a": 1}},
    )


async def fetch_operation(idempotency_key: str):
    async with AsyncSessionLocal() as session:
        repository = PlatformProviderOperationRepository(session)
        await repository.set_tenant_context(uuid.UUID(ORG_1))
        return await repository.get_by_idempotency(
            organization_id=uuid.UUID(ORG_1),
            provider_code="fake",
            idempotency_key=idempotency_key,
        )


@pytest.mark.asyncio
async def test_canonical_request_hash_is_stable_and_detects_material_change():
    first = make_request(metadata={"z": 1, "a": {"b": 2, "a": 1}})
    same = replace(first, metadata={"a": {"a": 1, "b": 2}, "z": 1})
    changed_amount = replace(first, amount_minor=100000)
    changed_currency = replace(first, currency_code="USD")
    changed_operation = replace(first, operation_type="payment_method_update")
    changed_org = replace(first, organization_id=uuid.UUID("81000000-0000-0000-0000-000000000099"))

    assert compute_provider_request_hash(first) == compute_provider_request_hash(same)
    assert compute_provider_request_hash(first) != compute_provider_request_hash(changed_amount)
    assert compute_provider_request_hash(first) != compute_provider_request_hash(changed_currency)
    assert compute_provider_request_hash(first) != compute_provider_request_hash(changed_operation)
    assert compute_provider_request_hash(first) != compute_provider_request_hash(changed_org)


@pytest.mark.asyncio
async def test_new_operation_reserves_calls_provider_and_records_success():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider(outcome=ProviderOutcomeKind.SUCCESS)
    service = PlatformProviderOperationService(provider)

    result = await service.execute(make_request())

    assert result.status == "succeeded"
    assert result.provider_called is True
    assert result.external_operation_ref is not None
    assert len(provider.calls) == 1
    stored = await fetch_operation("phase4b-key")
    assert stored is not None
    assert stored.status == "succeeded"
    assert stored.attempt_count == 1
    assert stored.completed_at is not None
    assert stored.canonical_request_sha256 == compute_provider_request_hash(make_request())


@pytest.mark.asyncio
async def test_same_key_same_request_replays_success_without_second_provider_call():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider(outcome=ProviderOutcomeKind.SUCCESS)
    service = PlatformProviderOperationService(provider)
    request = make_request(idempotency_key="replay-success")

    first = await service.execute(request)
    second = await service.execute(request)

    assert first.operation_id == second.operation_id
    assert second.status == "succeeded"
    assert second.provider_called is False
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_same_key_different_request_raises_conflict_without_provider_call():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider()
    service = PlatformProviderOperationService(provider)

    await service.reserve_operation(make_request(idempotency_key="conflict"))
    with pytest.raises(IdempotencyConflict):
        await service.execute(make_request(idempotency_key="conflict", amount_minor=100001))

    assert provider.calls == []


@pytest.mark.asyncio
async def test_existing_reserved_operation_is_claimed_once_and_then_executed():
    await cleanup_phase1_tables()
    await seed_organizations()
    request = make_request(idempotency_key="reserved-resume")
    request_hash = compute_provider_request_hash(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        await session.execute(
            text(
                """
                INSERT INTO platform_provider_operations (
                    organization_id, provider_code, operation_type,
                    idempotency_key, canonical_request_sha256, status
                )
                VALUES (
                    :org1, 'fake', 'checkout_session_reserve',
                    'reserved-resume', :request_hash, 'reserved'
                )
                """
            ),
            {"org1": ORG_1, "request_hash": request_hash},
        )
        await session.commit()

    provider = DeterministicFakeProvider()
    result = await PlatformProviderOperationService(provider).execute(request)

    assert result.status == "succeeded"
    assert result.provider_called is True
    assert len(provider.calls) == 1
    stored = await fetch_operation("reserved-resume")
    assert stored is not None
    assert stored.attempt_count == 1


@pytest.mark.asyncio
async def test_concurrent_identical_requests_create_one_row_and_one_provider_call():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider()
    service = PlatformProviderOperationService(provider)
    request = make_request(idempotency_key="concurrent")

    results = await asyncio.gather(
        service.execute(request),
        service.execute(request),
    )

    operation_ids = {result.operation_id for result in results}
    assert len(operation_ids) == 1
    assert len(provider.calls) == 1
    stored = await fetch_operation("concurrent")
    assert stored is not None
    assert stored.status == "succeeded"


@pytest.mark.asyncio
async def test_provider_call_happens_after_reservation_commit_without_db_session_or_row_lock():
    await cleanup_phase1_tables()
    await seed_organizations()
    sessions = []
    lock_probe_results: list[bool] = []

    def tracking_session_factory():
        session = AsyncSessionLocal()
        sessions.append(session)
        return session

    async def transaction_probe(call_request: ProviderCallRequest) -> bool:
        async with AsyncSessionLocal() as probe_session:
            await probe_session.execute(
                text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
                {"org1": ORG_1},
            )
            try:
                await probe_session.execute(
                    text(
                        """
                        SELECT id
                        FROM platform_provider_operations
                        WHERE id = :operation_id
                        FOR UPDATE NOWAIT
                        """
                    ),
                    {"operation_id": call_request.operation_id},
                )
                lock_probe_results.append(True)
            except Exception:
                lock_probe_results.append(False)
            finally:
                await probe_session.rollback()
        return any(session.in_transaction() for session in sessions)

    provider = DeterministicFakeProvider(
        outcome=ProviderOutcomeKind.SUCCESS,
        transaction_probe=transaction_probe,
    )
    service = PlatformProviderOperationService(
        provider,
        session_factory=tracking_session_factory,
    )

    result = await service.execute(make_request(idempotency_key="tx-boundary"))

    assert result.status == "succeeded"
    assert len(provider.calls) == 1
    assert provider.calls[0].active_transaction_observed is False
    assert not hasattr(provider.calls[0].request, "db")
    assert lock_probe_results == [True]
    assert len(sessions) == 2
    assert all(not session.in_transaction() for session in sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error"),
    [
        (ProviderOutcomeKind.SUCCESS, "succeeded", None),
        (ProviderOutcomeKind.BUSINESS_FAILURE, "failed", "provider_declined"),
        (ProviderOutcomeKind.RETRYABLE_FAILURE, "failed", "provider_retryable_failure"),
        (ProviderOutcomeKind.TIMEOUT, "unknown", "provider_timeout"),
        (ProviderOutcomeKind.UNKNOWN, "unknown", "provider_outcome_unknown"),
    ],
)
async def test_deterministic_fake_provider_outcomes(outcome, expected_status, expected_error):
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider(outcome=outcome)
    service = PlatformProviderOperationService(provider)

    result = await service.execute(make_request(idempotency_key=f"outcome-{outcome.value}"))

    assert result.status == expected_status
    assert result.error_classification == expected_error
    assert len(provider.calls) == 1
    if outcome is ProviderOutcomeKind.SUCCESS:
        assert result.external_operation_ref is not None
        assert result.external_operation_ref.startswith("fake_op_")
    assert provider.calls[0].request.operation_id == result.operation_id


class RaisingProvider:
    def __init__(self):
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        raise RuntimeError("provider transport interrupted")


@pytest.mark.asyncio
async def test_unexpected_provider_exception_records_unknown_without_raw_exception():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = RaisingProvider()
    request = make_request(idempotency_key="unexpected-provider-exception")

    result = await PlatformProviderOperationService(provider).execute(request)

    assert result.status == "unknown"
    assert result.error_classification == "provider_unexpected_exception"
    assert provider.calls == 1
    stored = await fetch_operation("unexpected-provider-exception")
    assert stored is not None
    assert stored.status == "unknown"
    assert stored.error_classification == "provider_unexpected_exception"
    assert "transport interrupted" not in str(stored)


@pytest.mark.asyncio
async def test_failure_and_unknown_replay_do_not_call_provider_again():
    await cleanup_phase1_tables()
    await seed_organizations()
    failed_provider = DeterministicFakeProvider(outcome=ProviderOutcomeKind.BUSINESS_FAILURE)
    failed_service = PlatformProviderOperationService(failed_provider)
    failed_request = make_request(idempotency_key="replay-failure")

    failed = await failed_service.execute(failed_request)
    failed_replay = await failed_service.execute(failed_request)

    assert failed.status == "failed"
    assert failed_replay.provider_called is False
    assert len(failed_provider.calls) == 1

    unknown_provider = DeterministicFakeProvider(outcome=ProviderOutcomeKind.UNKNOWN)
    unknown_service = PlatformProviderOperationService(unknown_provider)
    unknown_request = make_request(idempotency_key="replay-unknown")

    unknown = await unknown_service.execute(unknown_request)
    unknown_replay = await unknown_service.execute(unknown_request)

    assert unknown.status == "unknown"
    assert unknown_replay.provider_called is False
    assert len(unknown_provider.calls) == 1


class FailingRecordService(PlatformProviderOperationService):
    async def record_result(self, *, organization_id, result):
        raise ProviderResultPersistenceFailure("forced persistence failure")


@pytest.mark.asyncio
async def test_provider_success_with_result_persistence_failure_is_recoverable_without_duplicate_call():
    await cleanup_phase1_tables()
    await seed_organizations()
    provider = DeterministicFakeProvider(outcome=ProviderOutcomeKind.SUCCESS)
    request = make_request(idempotency_key="record-failure")
    failing = FailingRecordService(provider)

    with pytest.raises(ProviderResultPersistenceFailure):
        await failing.execute(request)

    stored = await fetch_operation("record-failure")
    assert stored is not None
    assert stored.status == "in_progress"
    assert stored.attempt_count == 1
    assert len(provider.calls) == 1

    retry = await PlatformProviderOperationService(provider).execute(request)
    assert retry.status == "in_progress"
    assert retry.provider_called is False
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_status_transitions_are_explicit_and_conflicting_terminal_result_is_rejected():
    await cleanup_phase1_tables()
    await seed_organizations()
    service = PlatformProviderOperationService(DeterministicFakeProvider())
    request = make_request(idempotency_key="transition")
    reserved = await service.reserve_operation(request)

    success = result_for_outcome(
        reserved.id,
        await DeterministicFakeProvider(outcome=ProviderOutcomeKind.SUCCESS).execute(
            ProviderCallRequest(
                operation_id=reserved.id,
                organization_id=request.organization_id,
                provider_code=request.provider_code,
                operation_type=request.operation_type,
                amount_minor=request.amount_minor,
                currency_code=request.currency_code,
                plan_version_id=request.plan_version_id,
                price_id=request.price_id,
                provider_customer_ref=request.provider_customer_ref,
                provider_payment_method_ref=request.provider_payment_method_ref,
                metadata=request.metadata,
            )
        ),
    )
    recorded = await service.record_result(
        organization_id=request.organization_id,
        result=success,
    )
    repeated = await service.record_result(
        organization_id=request.organization_id,
        result=success,
    )
    assert recorded.status == "succeeded"
    assert repeated.status == "succeeded"

    conflicting = replace(success, status="failed", error_classification="provider_declined")
    with pytest.raises(IllegalProviderOperationTransition):
        await service.record_result(
            organization_id=request.organization_id,
            result=conflicting,
        )

    conflicting_unknown = replace(success, status="unknown", error_classification="late_unknown")
    with pytest.raises(IllegalProviderOperationTransition):
        await service.record_result(
            organization_id=request.organization_id,
            result=conflicting_unknown,
        )

    unknown_request = make_request(idempotency_key="unknown-transition")
    unknown = await PlatformProviderOperationService(
        DeterministicFakeProvider(outcome=ProviderOutcomeKind.UNKNOWN)
    ).execute(unknown_request)
    resolving_success = replace(
        success,
        operation_id=unknown.operation_id,
        external_operation_ref="fake_op_resolved_later",
    )
    resolved = await service.record_result(
        organization_id=unknown_request.organization_id,
        result=resolving_success,
    )
    assert resolved.status == "succeeded"


def test_phase4b_safety_guardrails_remain_invisible_and_non_enforcing():
    from app.core.config import settings

    assert settings.PLATFORM_BILLING_CHECKOUT is False
    assert settings.PLATFORM_BILLING_WEBHOOK_PROCESSING is False
    assert settings.PLATFORM_BILLING_DUNNING_TRANSITIONS is False
    assert settings.PLATFORM_BILLING_NOTIFICATIONS is False
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "completion.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "simulation.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "callback.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "webhooks.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "tasks" / "reconciliation.py").exists()

    provider_source = (REPO_ROOT / "app" / "platform_billing" / "providers" / "fake.py").read_text(encoding="utf-8")
    forbidden = ["requests.", "httpx.", "razorpay", "cashfree", "stripe", "secret_key", "api_key"]
    for token in forbidden:
        assert token not in provider_source

    operation_source = (REPO_ROOT / "app" / "platform_billing" / "services" / "provider_operations.py").read_text(encoding="utf-8")
    assert "PlatformSubscription" not in operation_source
    assert "PlatformEntitlementProjection" not in operation_source
    assert "PlatformAccessProjection" not in operation_source
