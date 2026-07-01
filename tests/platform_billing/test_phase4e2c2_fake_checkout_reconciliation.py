from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from app.main import app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.provider_operations import ProviderOperationResult
from app.platform_billing.domain.reconciliation import (
    PROVIDER_TERMINAL_SUCCEEDED,
    ProviderOperationEvidence,
    ReconciliationPage,
    ReconciliationRunRequest,
    compute_evidence_hash,
)
from app.platform_billing.models.provider import PlatformProviderOperation
from app.platform_billing.providers.fake_checkout_evidence import (
    LocalEncryptedFakeCheckoutEvidenceStore,
    LocalFakeCheckoutProviderEvidenceReader,
    build_pending_evidence,
    build_terminal_evidence,
)
from app.platform_billing.repositories.provider_operations import PlatformProviderOperationRepository
from app.platform_billing.repositories.webhooks import PlatformWebhookInboxRepository
from app.platform_billing.services.checkout_simulation import CheckoutSimulationServices, default_simulation_services
from app.platform_billing.services.reconciliation import (
    DEFAULT_FAKE_CHECKOUT_RECONCILIATION_DELAY,
    DEFAULT_RECONCILIATION_LEASE,
    FakeCheckoutReconciliationDisabled,
    PlatformReconciliationService,
    reconcile_fake_checkout_operations,
)
from app.platform_billing.services.webhooks import PlatformWebhookProcessingService
from tests.platform_billing.test_phase1_schema import cleanup_phase1_tables, seed_organizations
from tests.platform_billing.test_phase4e1_fake_checkout_api import (
    ADMIN_ORG_ID,
    OWNER_ORG_ID,
    _cleanup,
    _post_checkout,
    admin_token_headers,
    platform_catalog,
    setup_test_db_override,
)
from tests.platform_billing.test_phase4e2_fake_checkout_simulation import (
    CountingFakeCheckoutOutcomeProducer,
    CountingPayloadStore,
    _clear_simulation_services_override,
    _enable_simulation,
    _simulate,
)
from tests.platform_billing.test_phase4e2c1_fake_provider_evidence import (
    _confirm_operation,
    _ensure_fake_customer,
    _webhook_inbox_count,
)


NOW = datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=10)
NEW = NOW + timedelta(minutes=10)
SHA = "a" * 64


class MutableClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture(autouse=True)
def _enable_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_PROVIDER_MODE", "fake")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "provider-evidence"))


async def _seed_confirm(
    *,
    org_id: uuid.UUID = ADMIN_ORG_ID,
    status: str = "in_progress",
    provider_code: str = "fake",
    operation_type: str = "confirm_checkout",
    external_ref: str | None = None,
    include_external_ref: bool = True,
    updated_at: datetime = OLD,
    completed_at: datetime | None = None,
) -> PlatformProviderOperation:
    operation_id = uuid.uuid4()
    external_ref = (external_ref or f"fake_confirm_{operation_id.hex}_e2c2") if include_external_ref else None
    terminal = status in {"succeeded", "failed", "unknown"}
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES (:org_id, :name, :slug, 'basic', true, 1, 'USD')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "org_id": str(org_id),
                "name": f"E2C2 Org {org_id.hex[:8]}",
                "slug": f"e2c2-org-{org_id.hex[:8]}",
            },
        )
        row = PlatformProviderOperation(
            id=operation_id,
            organization_id=org_id,
            provider_code=provider_code,
            operation_type=operation_type,
            idempotency_key=f"e2c2-{uuid.uuid4().hex}",
            canonical_request_sha256=SHA,
            status=status,
            external_operation_ref=external_ref,
            attempt_count=1,
            result_evidence_sha256=SHA if terminal else None,
            result_reference=f"seed:{status}" if terminal else None,
            error_classification="seed_failure" if status == "failed" else None,
            completed_at=(completed_at or updated_at) if terminal else None,
            updated_at=updated_at,
        )
        session.add(row)
        await session.commit()
        return row


async def _fetch_operation(operation_id: uuid.UUID, org_id: uuid.UUID = ADMIN_ORG_ID):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        row = await session.get(PlatformProviderOperation, operation_id)
        assert row is not None
        return row


async def _fetch_item(external_ref: str, org_id: uuid.UUID = ADMIN_ORG_ID):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        result = await session.execute(
            text(
                """
                SELECT resolution_status, last_error_code, discrepancy_classification
                FROM platform_reconciliation_items
                WHERE external_object_ref = :external_ref
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"external_ref": external_ref},
        )
        return result.mappings().one_or_none()


async def _side_effect_counts() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        tables = {
            "subscriptions": "platform_subscriptions",
            "entitlements": "platform_entitlement_projection",
            "access_projection": "platform_access_projection",
            "usage_projection": "platform_usage_projection",
            "payment_methods": "platform_payment_methods",
        }
        return {
            name: int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for name, table in tables.items()
        }


async def _record_evidence(
    operation: PlatformProviderOperation,
    *,
    outcome: str,
    checkout_id: uuid.UUID | None = None,
    event_id: str | None = None,
) -> None:
    checkout_id = checkout_id or uuid.uuid4()
    store = LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))
    if outcome == "pending":
        evidence = build_pending_evidence(
            organization_id=operation.organization_id,
            confirm_checkout_operation_id=operation.id,
            checkout_operation_id=checkout_id,
            external_operation_ref=operation.external_operation_ref,
            checkout_session_reference=f"session_{checkout_id.hex[:12]}",
            provider_customer_ref=f"cus_{operation.organization_id.hex[:12]}",
            observed_at=NOW,
        )
    else:
        terminal_timestamp = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
        evidence = build_terminal_evidence(
            organization_id=operation.organization_id,
            confirm_checkout_operation_id=operation.id,
            checkout_operation_id=checkout_id,
            external_operation_ref=operation.external_operation_ref,
            checkout_session_reference=f"session_{checkout_id.hex[:12]}",
            provider_customer_ref=f"cus_{operation.organization_id.hex[:12]}",
            provider_outcome=outcome,
            provider_event_id=event_id or f"evt_{outcome}_{operation.id.hex[:12]}",
            raw_event=f'{{"outcome":"{outcome}"}}'.encode(),
            signature_header=f"v1={outcome}",
            signature_timestamp=terminal_timestamp,
        )
    return await store.record(evidence)


async def _run(clock: MutableClock | None = None, org_id: uuid.UUID = ADMIN_ORG_ID):
    return await reconcile_fake_checkout_operations(
        organization_id=org_id,
        clock=clock or MutableClock(),
        eligibility_delay=DEFAULT_FAKE_CHECKOUT_RECONCILIATION_DELAY,
    )


def _request_for(operation: PlatformProviderOperation) -> ReconciliationRunRequest:
    return ReconciliationRunRequest(
        provider_code="fake",
        organization_id=operation.organization_id,
        scope={"external_operation_refs": [operation.external_operation_ref]},
        watermark={"test": "phase4e2c2"},
    )


def _local_reader() -> LocalFakeCheckoutProviderEvidenceReader:
    return LocalFakeCheckoutProviderEvidenceReader(
        LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))
    )


class CountingReader(LocalFakeCheckoutProviderEvidenceReader):
    def __init__(self):
        super().__init__(LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR)))
        self.list_calls = 0
        self.fetch_calls = 0

    async def list_operation_evidence(self, request):
        self.list_calls += 1
        return await super().list_operation_evidence(request)

    async def fetch_operation_evidence(self, evidence_ref):
        self.fetch_calls += 1
        return await super().fetch_operation_evidence(evidence_ref)


class SingleEvidenceReader:
    def __init__(self, evidence: ProviderOperationEvidence):
        self.evidence = evidence
        self.list_calls = 0
        self.fetch_calls = 0

    async def list_operation_evidence(self, request):
        self.list_calls += 1
        return ReconciliationPage(evidence=(self.evidence,), next_watermark=dict(request.watermark))

    async def fetch_operation_evidence(self, evidence_ref):
        self.fetch_calls += 1
        return self.evidence


class CountingLocalEvidenceStore(LocalEncryptedFakeCheckoutEvidenceStore):
    def __init__(self, root_dir: Path):
        super().__init__(root_dir)
        self.record_calls = 0

    async def record(self, evidence):
        self.record_calls += 1
        return await super().record(evidence)


def _install_durable_simulation_services(payload_store=None):
    evidence_store = CountingLocalEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))
    producer = CountingFakeCheckoutOutcomeProducer()
    payload_store = payload_store or CountingPayloadStore()
    services = CheckoutSimulationServices(
        event_producer=producer,
        payload_store=payload_store,
        evidence_store=evidence_store,
    )
    app.dependency_overrides[default_simulation_services] = lambda: services
    return services, evidence_store, producer, payload_store


async def _create_checkout_and_simulate(
    client,
    headers,
    platform_catalog,
    *,
    outcome: str = "succeeded",
    id_prefix: str = "e2c2",
):
    checkout_response = await _post_checkout(
        client,
        headers,
        idempotency_key=f"{id_prefix}-checkout-{uuid.uuid4().hex[:12]}",
        plan_code=platform_catalog["plan_code"],
    )
    assert checkout_response.status_code == 200, checkout_response.text
    checkout = checkout_response.json()
    simulation = await _simulate(
        client,
        headers,
        checkout_operation_id=checkout["operation_id"],
        outcome=outcome,
        key=f"{id_prefix}-simulate-{uuid.uuid4().hex[:12]}",
    )
    return checkout, simulation


async def _inbox_rows() -> list[dict]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                """
                SELECT id, provider_event_id, processing_status, error_classification,
                       encrypted_payload_ref
                FROM platform_webhook_inbox
                ORDER BY created_at, id
                """
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _run_e2c2_with_counters(monkeypatch, operation):
    read_calls = []
    record_calls = []
    original_fetch = LocalFakeCheckoutProviderEvidenceReader.fetch_operation_evidence
    original_record = PlatformProviderOperationRepository.record_result

    async def counting_fetch(self, evidence_ref):
        read_calls.append(evidence_ref)
        return await original_fetch(self, evidence_ref)

    async def counting_record(self, result):
        record_calls.append(result)
        return await original_record(self, result)

    monkeypatch.setattr(LocalFakeCheckoutProviderEvidenceReader, "fetch_operation_evidence", counting_fetch)
    monkeypatch.setattr(PlatformProviderOperationRepository, "record_result", counting_record)
    before = await _side_effect_counts()
    result = await _run(MutableClock(datetime.now(timezone.utc) + timedelta(minutes=5)))
    after = await _side_effect_counts()
    refreshed = await _fetch_operation(operation.id)
    return {
        "result": result,
        "read_calls": read_calls,
        "record_calls": record_calls,
        "before_side_effects": before,
        "after_side_effects": after,
        "operation": refreshed,
    }


async def _age_operation_for_reconciliation(operation_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE platform_provider_operations
                SET created_at = :created_at,
                    updated_at = :updated_at
                WHERE id = :operation_id
                """
            ),
            {"operation_id": operation_id, "created_at": OLD, "updated_at": OLD},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_lost_delivery_recovery_without_webhook_inbox_or_subscription_mutation():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")

    result = await _run()

    refreshed = await _fetch_operation(operation.id)
    assert result.processed == 1
    assert refreshed.status == "succeeded"
    assert refreshed.result_reference.startswith("fake-provider-evidence:v1:")
    item = await _fetch_item(operation.external_operation_ref)
    assert item["resolution_status"] == "resolved"
    async with AsyncSessionLocal() as session:
        assert await session.scalar(text("SELECT count(*) FROM platform_webhook_inbox")) == 0
        assert await session.scalar(text("SELECT count(*) FROM platform_subscriptions")) == 0
        assert await session.scalar(text("SELECT count(*) FROM platform_entitlement_projection")) == 0
        assert await session.scalar(text("SELECT count(*) FROM platform_access_projection")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "expected_status"), [("succeeded", "succeeded"), ("failed", "failed")])
async def test_successful_recovery_direct_side_effect_counts(outcome, expected_status, monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome=outcome)
    before = await _side_effect_counts()
    calls = []
    original = PlatformProviderOperationRepository.record_result

    async def counting_record_result(self, result):
        calls.append(result)
        return await original(self, result)

    monkeypatch.setattr(PlatformProviderOperationRepository, "record_result", counting_record_result)
    reader = CountingReader()
    service = PlatformReconciliationService(evidence_reader=reader, clock=MutableClock())

    result = await service.reconcile(_request_for(operation))

    refreshed = await _fetch_operation(operation.id)
    assert result.processed == 1
    assert reader.fetch_calls == 1
    assert len(calls) == 1
    assert calls[0].status == expected_status
    assert refreshed.status == expected_status
    assert await _side_effect_counts() == before


@pytest.mark.asyncio
async def test_replay_after_success_has_no_additional_terminal_transition(monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")
    original = PlatformProviderOperationRepository.record_result
    calls = []

    async def counting_record_result(self, result):
        calls.append(result)
        return await original(self, result)

    monkeypatch.setattr(PlatformProviderOperationRepository, "record_result", counting_record_result)
    reader = CountingReader()
    service = PlatformReconciliationService(evidence_reader=reader, clock=MutableClock())

    first = await service.reconcile(_request_for(operation))
    second = await service.reconcile(_request_for(operation))

    assert first.processed == 1
    assert second.discovered == 0
    assert reader.fetch_calls == 1
    assert len(calls) == 1
    assert (await _fetch_operation(operation.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_pending_not_found_and_corrupt_evidence_have_zero_terminal_mutations(monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    pending = await _seed_confirm(status="in_progress", external_ref="fake_confirm_pending_counts")
    absent = await _seed_confirm(status="in_progress", external_ref="fake_confirm_absent_counts")
    corrupt = await _seed_confirm(status="in_progress", external_ref="fake_confirm_corrupt_counts")
    await _record_evidence(pending, outcome="pending")
    corrupt_evidence = await _record_evidence(corrupt, outcome="succeeded")
    store = LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))
    evidence_file = store._path_for("fake", corrupt_evidence.organization_id, corrupt_evidence.external_operation_ref)
    evidence_file.write_bytes(evidence_file.read_bytes()[:16])
    original = PlatformProviderOperationRepository.record_result
    calls = []

    async def counting_record_result(self, result):
        calls.append(result)
        return await original(self, result)

    monkeypatch.setattr(PlatformProviderOperationRepository, "record_result", counting_record_result)

    pending_reader = CountingReader()
    pending_result = await PlatformReconciliationService(evidence_reader=pending_reader, clock=MutableClock()).reconcile(_request_for(pending))
    absent_reader = CountingReader()
    absent_result = await PlatformReconciliationService(evidence_reader=absent_reader, clock=MutableClock()).reconcile(_request_for(absent))
    corrupt_reader = CountingReader()
    corrupt_result = await PlatformReconciliationService(evidence_reader=corrupt_reader, clock=MutableClock()).reconcile(_request_for(corrupt))

    assert pending_reader.fetch_calls == 1
    assert absent_reader.fetch_calls == 1
    assert corrupt_reader.fetch_calls == 1
    assert pending_result.run.status == "running"
    assert absent_result.run.status == "running"
    assert corrupt_result.run.status == "succeeded"
    assert calls == []
    assert (await _fetch_operation(pending.id)).status == "in_progress"
    assert (await _fetch_operation(absent.id)).status == "in_progress"
    assert (await _fetch_operation(corrupt.id)).status == "in_progress"
    assert (await _fetch_item(pending.external_operation_ref))["last_error_code"] == "provider_evidence_pending"
    assert (await _fetch_item(absent.external_operation_ref))["last_error_code"] == "provider_evidence_not_found"
    assert (await _fetch_item(corrupt.external_operation_ref))["last_error_code"] == "ambiguous_provider_evidence"


@pytest.mark.asyncio
async def test_same_evidence_identity_different_hash_is_conflict_without_overwrite(monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(
        status="succeeded",
        external_ref="fake_confirm_same_identity_diff_hash",
        completed_at=OLD,
    )
    evidence_ref = "fake-provider-evidence:v1:fake:identity-conflict"
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(operation.organization_id)},
        )
        row = await session.get(PlatformProviderOperation, operation.id)
        row.result_reference = evidence_ref
        row.result_evidence_sha256 = "1" * 64
        await session.commit()
    safe = {
        "confirm_checkout_operation_id": str(operation.id),
        "external_operation_ref": operation.external_operation_ref,
        "provider_code": "fake",
        "provider_status": PROVIDER_TERMINAL_SUCCEEDED,
    }
    evidence = ProviderOperationEvidence(
        provider_code="fake",
        external_operation_ref=operation.external_operation_ref,
        provider_status=PROVIDER_TERMINAL_SUCCEEDED,
        observed_at=NEW,
        evidence_ref=evidence_ref,
        evidence_sha256=compute_evidence_hash(safe),
        safe_evidence=safe,
    )
    calls = []
    original = PlatformProviderOperationRepository.record_result

    async def counting_record_result(self, result):
        calls.append(result)
        return await original(self, result)

    monkeypatch.setattr(PlatformProviderOperationRepository, "record_result", counting_record_result)
    reader = SingleEvidenceReader(evidence)
    result = await PlatformReconciliationService(evidence_reader=reader, clock=MutableClock()).reconcile(_request_for(operation))

    refreshed = await _fetch_operation(operation.id)
    assert result.failed == 1
    assert reader.fetch_calls == 1
    assert calls == []
    assert refreshed.status == "succeeded"
    assert refreshed.result_evidence_sha256 == "1" * 64
    assert (await _fetch_item(operation.external_operation_ref))["last_error_code"] == "evidence_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "expected_status"), [("succeeded", "succeeded"), ("failed", "failed")])
async def test_terminal_provider_outcomes_recover_confirm_checkout(outcome, expected_status):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="unknown")
    await _record_evidence(operation, outcome=outcome)

    await _run()

    refreshed = await _fetch_operation(operation.id)
    assert refreshed.status == expected_status
    assert refreshed.result_evidence_sha256 is not None


@pytest.mark.asyncio
async def test_pending_and_absent_provider_evidence_are_retryable_noops():
    await cleanup_phase1_tables()
    await seed_organizations()
    pending = await _seed_confirm(status="in_progress", external_ref="fake_confirm_pending_e2c2")
    absent = await _seed_confirm(status="unknown", external_ref="fake_confirm_absent_e2c2")
    await _record_evidence(pending, outcome="pending")

    result = await _run()

    assert result.run.status == "running"
    assert (await _fetch_operation(pending.id)).status == "in_progress"
    assert (await _fetch_operation(absent.id)).status == "unknown"
    assert (await _fetch_item(pending.external_operation_ref))["last_error_code"] == "provider_evidence_pending"
    assert (await _fetch_item(absent.external_operation_ref))["last_error_code"] == "provider_evidence_not_found"


@pytest.mark.asyncio
async def test_candidate_selection_excludes_wrong_or_too_new_operations():
    await cleanup_phase1_tables()
    await seed_organizations()
    eligible = await _seed_confirm(status="in_progress", external_ref="fake_confirm_eligible_e2c2")
    eligible_unknown = await _seed_confirm(status="unknown", external_ref="fake_confirm_unknown_e2c2")
    missing_ref = await _seed_confirm(status="in_progress", include_external_ref=False)
    excluded = [
        await _seed_confirm(status="in_progress", provider_code="stripe", external_ref="fake_confirm_wrong_provider"),
        await _seed_confirm(status="in_progress", operation_type="create_checkout", external_ref="fake_confirm_create"),
        await _seed_confirm(status="in_progress", operation_type="refund", external_ref="fake_confirm_refund"),
        await _seed_confirm(status="reserved", external_ref="fake_confirm_reserved"),
        missing_ref,
        await _seed_confirm(status="in_progress", external_ref="fake_confirm_new", updated_at=NOW),
        await _seed_confirm(status="in_progress", external_ref="malformed_reference"),
        await _seed_confirm(status="in_progress", org_id=OWNER_ORG_ID, external_ref="fake_confirm_cross_tenant"),
    ]
    await _record_evidence(eligible, outcome="succeeded")
    await _record_evidence(eligible_unknown, outcome="failed")
    for operation in excluded:
        if operation.provider_code == "fake" and operation.external_operation_ref:
            await _record_evidence(operation, outcome="succeeded")

    result = await _run()

    assert result.processed == 2
    assert (await _fetch_operation(eligible.id)).status == "succeeded"
    assert (await _fetch_operation(eligible_unknown.id)).status == "failed"
    for operation in excluded:
        assert (await _fetch_operation(operation.id, operation.organization_id)).status == operation.status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED", False),
        ("PLATFORM_BILLING_PROVIDER_MODE", "disabled"),
        ("PLATFORM_BILLING_PROVIDER_MODE", "stripe"),
        ("ENVIRONMENT", "production"),
        ("ENVIRONMENT", "staging"),
        ("ENVIRONMENT", ""),
        ("ENVIRONMENT", "unknown"),
        ("ENVIRONMENT", "prodution"),
        ("PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", ""),
    ],
)
async def test_gate_denials_do_not_read_or_mutate(setting, value, monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    monkeypatch.setattr(settings, setting, value)

    with pytest.raises(FakeCheckoutReconciliationDisabled):
        await _run()

    assert (await _fetch_operation(operation.id)).status == "in_progress"
    assert await _fetch_item(operation.external_operation_ref) is None


@pytest.mark.asyncio
async def test_gate_denies_unusable_path_and_allows_development_and_test(tmp_path, monkeypatch):
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    blocked = tmp_path / "blocked-file"
    blocked.write_text("not a directory")
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(blocked))
    with pytest.raises(FakeCheckoutReconciliationDisabled):
        await _run()

    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "provider-dev"))
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    await _record_evidence(operation, outcome="pending")
    dev_result = await _run()
    assert dev_result.processed == 1

    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "provider-test"))
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    await _record_evidence(operation, outcome="pending")
    test_result = await _run()
    assert test_result.processed == 1


@pytest.mark.asyncio
async def test_wrong_confirm_operation_identity_is_ambiguous_and_does_not_update():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    wrong_confirm_id = uuid.uuid4()
    evidence = build_terminal_evidence(
        organization_id=operation.organization_id,
        confirm_checkout_operation_id=wrong_confirm_id,
        checkout_operation_id=uuid.uuid4(),
        external_operation_ref=operation.external_operation_ref,
        checkout_session_reference="wrong_session",
        provider_customer_ref="wrong_customer",
        provider_outcome="succeeded",
        provider_event_id="evt_wrong_confirm",
        raw_event=b"wrong",
        signature_header="v1=wrong",
        signature_timestamp=int(NOW.timestamp()),
    )
    await LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR)).record(evidence)

    await _run()

    assert (await _fetch_operation(operation.id)).status == "in_progress"
    assert (await _fetch_item(operation.external_operation_ref))["last_error_code"] == "ambiguous_provider_evidence"


@pytest.mark.asyncio
async def test_matching_and_conflicting_terminal_evidence_never_overwrite_terminal_local_state():
    await cleanup_phase1_tables()
    await seed_organizations()
    matching = await _seed_confirm(status="succeeded", external_ref="fake_confirm_terminal_match", completed_at=OLD)
    conflicting = await _seed_confirm(status="failed", external_ref="fake_confirm_terminal_conflict", completed_at=OLD)
    await _record_evidence(matching, outcome="succeeded")
    await _record_evidence(conflicting, outcome="succeeded")

    await _run()

    assert (await _fetch_operation(matching.id)).status == "succeeded"
    assert (await _fetch_operation(conflicting.id)).status == "failed"
    assert (await _fetch_item("fake_confirm_terminal_match")) is None
    assert (await _fetch_item("fake_confirm_terminal_conflict")) is None


@pytest.mark.asyncio
async def test_two_reconciliation_workers_one_terminal_transition():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")

    results = await asyncio.gather(_run(), _run())

    refreshed = await _fetch_operation(operation.id)
    assert refreshed.status == "succeeded"
    assert sum(result.processed for result in results) == 1


@pytest.mark.asyncio
async def test_webhook_reconciliation_same_outcome_race_converges_idempotently():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")
    service = PlatformReconciliationService(evidence_reader=_local_reader(), clock=MutableClock())
    run = await service.reserve_run(_request_for(operation))
    run_claim = await service.claim_run(run.id)
    page = await service._evidence_reader.list_operation_evidence(_request_for(operation))
    item = (await service._discover_items(_request_for(operation), run_claim, page.evidence, dict(page.next_watermark)))[0]
    item_claim = await service.claim_item(item.id, organization_id=item.organization_id)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            repo = PlatformProviderOperationRepository(session)
            await repo.set_tenant_context(operation.organization_id)
            await repo.record_result(
                ProviderOperationResult(operation.id, "succeeded", operation.external_operation_ref, None, "b" * 64, "webhook:succeeded", False)
            )

    result = await service.process_item_claim(item_claim)

    assert result.classification == "already_consistent"
    assert (await _fetch_operation(operation.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_webhook_reconciliation_opposite_outcome_race_records_conflict_without_overwrite():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")
    service = PlatformReconciliationService(evidence_reader=_local_reader(), clock=MutableClock())
    run = await service.reserve_run(_request_for(operation))
    run_claim = await service.claim_run(run.id)
    page = await service._evidence_reader.list_operation_evidence(_request_for(operation))
    item = (await service._discover_items(_request_for(operation), run_claim, page.evidence, dict(page.next_watermark)))[0]
    item_claim = await service.claim_item(item.id, organization_id=item.organization_id)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            repo = PlatformProviderOperationRepository(session)
            await repo.set_tenant_context(operation.organization_id)
            await repo.record_result(
                ProviderOperationResult(operation.id, "failed", operation.external_operation_ref, "webhook_failed", "c" * 64, "webhook:failed", False)
            )

    result = await service.process_item_claim(item_claim)

    assert result.classification == "evidence_conflict"
    assert result.status == "failed"
    refreshed = await _fetch_operation(operation.id)
    assert refreshed.status == "failed"
    assert refreshed.result_reference == "webhook:failed"


@pytest.mark.asyncio
async def test_evidence_read_is_outside_transaction_and_before_fresh_result_transaction():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")
    sessions = []
    probes = []

    class ProbingReader(LocalFakeCheckoutProviderEvidenceReader):
        async def fetch_operation_evidence(self, evidence_ref):
            assert all(not session.in_transaction() for session in sessions)
            async with AsyncSessionLocal() as probe_session:
                try:
                    await probe_session.execute(
                        text(
                            "SELECT id FROM platform_provider_operations "
                            "WHERE id = :id FOR UPDATE NOWAIT"
                        ),
                        {"id": operation.id},
                    )
                    probes.append("operation_lock_free")
                finally:
                    await probe_session.rollback()
            return await super().fetch_operation_evidence(evidence_ref)

    def session_factory():
        session = AsyncSessionLocal()
        sessions.append(session)
        return session

    service = PlatformReconciliationService(
        evidence_reader=ProbingReader(LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))),
        session_factory=session_factory,
        clock=MutableClock(),
    )

    result = await service.reconcile(_request_for(operation))

    assert result.processed == 1
    assert probes == ["operation_lock_free"]
    assert all(not session.in_transaction() for session in sessions)


@pytest.mark.asyncio
async def test_stale_worker_fencing_cannot_overwrite_completed_item():
    await cleanup_phase1_tables()
    await seed_organizations()
    clock = MutableClock()
    operation = await _seed_confirm(status="in_progress")
    await _record_evidence(operation, outcome="succeeded")

    # The internal entry point uses Phase 4D item claims/fencing; a completed item cannot be reopened by a stale worker.
    await _run(clock)
    item = await _fetch_item(operation.external_operation_ref)
    assert item["resolution_status"] == "resolved"

    clock.advance(DEFAULT_RECONCILIATION_LEASE + timedelta(seconds=1))
    assert (await _fetch_operation(operation.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_payload_store_failure_is_recovered_by_e2c2_reconciliation(
    client,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session,
):
    class FailingPayloadStore(CountingPayloadStore):
        def __init__(self):
            super().__init__()
            self.write_attempts = 0

        async def put_verified_payload(self, **kwargs):
            self.write_attempts += 1
            raise RuntimeError("payload store unavailable")

    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    services, evidence_store, producer, payload_store = _install_durable_simulation_services(FailingPayloadStore())
    try:
        checkout_response = await _post_checkout(
            client,
            admin_token_headers,
            idempotency_key=f"e2c2-payload-checkout-{uuid.uuid4().hex[:12]}",
            plan_code=platform_catalog["plan_code"],
        )
        assert checkout_response.status_code == 200, checkout_response.text
        checkout = checkout_response.json()

        with pytest.raises(RuntimeError):
            await _simulate(
                client,
                admin_token_headers,
                checkout_operation_id=checkout["operation_id"],
                outcome="succeeded",
                key=f"e2c2-payload-sim-{uuid.uuid4().hex[:12]}",
            )

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=operation.external_operation_ref,
        )
        assert evidence_store.record_calls == 1
        assert evidence is not None
        assert evidence.provider_event_id == producer.events[0].provider_event_id
        assert payload_store.write_attempts == 1
        assert len(payload_store.payloads) == 0
        assert await _webhook_inbox_count(db_session) == 0
        assert operation.status == "in_progress"

        await _age_operation_for_reconciliation(operation.id)
        counters = await _run_e2c2_with_counters(monkeypatch, operation)
        assert len(counters["read_calls"]) == 1
        assert len(counters["record_calls"]) == 1
        assert counters["record_calls"][0].status == "succeeded"
        assert counters["operation"].status == "succeeded"
        assert counters["operation"].result_evidence_sha256 == evidence.canonical_evidence_hash
        assert counters["operation"].result_reference.endswith(operation.external_operation_ref)
        assert counters["after_side_effects"] == counters["before_side_effects"]
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_inbox_insertion_failure_is_recovered_by_e2c2_reconciliation(
    client,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    services, evidence_store, producer, payload_store = _install_durable_simulation_services(CountingPayloadStore())
    inbox_calls = []

    async def fail_accept(self, **kwargs):
        inbox_calls.append(kwargs)
        raise SQLAlchemyError("forced inbox insert failure")

    monkeypatch.setattr(PlatformWebhookInboxRepository, "accept", fail_accept)
    try:
        checkout_response = await _post_checkout(
            client,
            admin_token_headers,
            idempotency_key=f"e2c2-inbox-checkout-{uuid.uuid4().hex[:12]}",
            plan_code=platform_catalog["plan_code"],
        )
        assert checkout_response.status_code == 200, checkout_response.text
        checkout = checkout_response.json()

        with pytest.raises(Exception):
            await _simulate(
                client,
                admin_token_headers,
                checkout_operation_id=checkout["operation_id"],
                outcome="succeeded",
                key=f"e2c2-inbox-sim-{uuid.uuid4().hex[:12]}",
            )

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=operation.external_operation_ref,
        )
        assert evidence_store.record_calls == 1
        assert evidence is not None
        assert evidence.provider_event_id == producer.events[0].provider_event_id
        assert len(payload_store.put_calls) == 1
        assert len(inbox_calls) == 1
        assert payload_store.delete_calls == payload_store.put_calls
        assert len(payload_store.payloads) == 0
        assert await _webhook_inbox_count(db_session) == 0
        assert operation.status == "in_progress"

        await _age_operation_for_reconciliation(operation.id)
        counters = await _run_e2c2_with_counters(monkeypatch, operation)
        assert len(counters["read_calls"]) == 1
        assert len(counters["record_calls"]) == 1
        assert counters["record_calls"][0].status == "succeeded"
        assert counters["operation"].status == "succeeded"
        assert counters["operation"].result_evidence_sha256 == evidence.canonical_evidence_hash
        assert counters["after_side_effects"] == counters["before_side_effects"]
        reread = await evidence_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=operation.external_operation_ref,
        )
        assert reread.provider_event_id == evidence.provider_event_id
        assert evidence_store.record_calls == 1
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_webhook_processing_failure_converges_with_e2c2_reconciliation(
    client,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    services, evidence_store, producer, payload_store = _install_durable_simulation_services(CountingPayloadStore())
    original_apply = PlatformWebhookProcessingService._apply_event
    processing_failures = []

    async def fail_apply(self, event, claim):
        processing_failures.append((event.provider_event_id, claim.inbox_id))
        raise RuntimeError("forced processing failure")

    monkeypatch.setattr(PlatformWebhookProcessingService, "_apply_event", fail_apply)
    try:
        checkout, response = await _create_checkout_and_simulate(
            client,
            admin_token_headers,
            platform_catalog,
            outcome="succeeded",
            id_prefix="e2c2-processing",
        )
        assert response.status_code == 200, response.text
        assert response.json()["webhook_processing_status"] == "failed_retryable"

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        inboxes = await _inbox_rows()
        evidence = await evidence_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=operation.external_operation_ref,
        )
        assert evidence_store.record_calls == 1
        assert evidence is not None
        assert evidence.provider_event_id == producer.events[0].provider_event_id
        assert len(inboxes) == 1
        assert inboxes[0]["processing_status"] == "failed_retryable"
        assert operation.status == "in_progress"
        assert len(processing_failures) == 1

        await _age_operation_for_reconciliation(operation.id)
        counters = await _run_e2c2_with_counters(monkeypatch, operation)
        assert len(counters["read_calls"]) == 1
        assert len(counters["record_calls"]) == 1
        assert counters["record_calls"][0].status == "succeeded"
        assert counters["operation"].status == "succeeded"
        assert counters["operation"].result_evidence_sha256 == evidence.canonical_evidence_hash
        assert counters["after_side_effects"] == counters["before_side_effects"]

        before_retry = await _fetch_operation(operation.id)
        retry_record_count = len(counters["record_calls"])
        monkeypatch.setattr(PlatformWebhookProcessingService, "_apply_event", original_apply)
        retry = await PlatformWebhookProcessingService(payload_store=payload_store).process(inboxes[0]["id"])
        after_retry = await _fetch_operation(operation.id)
        assert retry.status == "processed"
        assert len(counters["record_calls"]) == retry_record_count + 1
        assert after_retry.status == "succeeded"
        assert after_retry.result_reference == before_retry.result_reference
        assert after_retry.result_evidence_sha256 == before_retry.result_evidence_sha256
        assert after_retry.completed_at == before_retry.completed_at
    finally:
        _clear_simulation_services_override()


def test_no_public_reconciliation_route_and_flag_disabled_by_default():
    from app.core.config import Settings

    assert Settings().PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED is False
    assert not (Path.cwd() / "app" / "platform_billing" / "api" / "reconciliation.py").exists()
