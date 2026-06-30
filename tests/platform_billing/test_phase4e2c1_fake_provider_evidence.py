from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import psycopg2
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.main import app
from app.platform_billing.models.provider import PlatformProviderOperation
from app.platform_billing.models.webhook import PlatformWebhookInbox
from app.platform_billing.providers.fake_checkout_evidence import (
    FakeCheckoutEvidenceConflict,
    FakeCheckoutEvidenceCorrupt,
    FakeCheckoutEvidenceStorageFailure,
    InMemoryFakeCheckoutEvidenceStore,
    LocalEncryptedFakeCheckoutEvidenceStore,
    LocalFakeCheckoutProviderEvidenceReader,
    build_pending_evidence,
    build_terminal_evidence,
    default_fake_checkout_evidence_reader,
)
from app.platform_billing.providers.fake_checkout_simulation import DeterministicFakeCheckoutOutcomeProducer
from app.platform_billing.services.checkout_simulation import CheckoutSimulationServices, default_simulation_services
from app.platform_billing.services.webhooks import PlatformWebhookAcceptanceService
from app.platform_billing.repositories.webhooks import PlatformWebhookInboxRepository
from app.platform_billing.webhooks.payload_store import InMemoryEncryptedWebhookPayloadStore, LocalEncryptedWebhookPayloadStore
from tests.platform_billing.test_phase4e1_fake_checkout_api import (
    ADMIN_ORG_ID,
    OWNER_ORG_ID,
    _post_checkout,
    admin_token_headers,
    platform_catalog,
    setup_test_db_override,
)


def _multiprocess_worker(root, spec, barrier, queue):
    async def run():
        store = LocalEncryptedFakeCheckoutEvidenceStore(Path(root))
        evidence = _evidence_from_spec(spec)
        barrier.wait(timeout=10)
        try:
            stored = await store.record(evidence)
            queue.put(
                {
                    "ok": True,
                    "outcome": stored.provider_outcome,
                    "event_id": stored.provider_event_id,
                    "evidence_hash": stored.canonical_evidence_hash,
                    "raw_hash": stored.raw_event_sha256,
                }
            )
        except Exception as exc:
            queue.put({"ok": False, "error": exc.__class__.__name__, "message": str(exc)})

    asyncio.run(run())


def _evidence_spec(*, org_id, external_ref, outcome, event_id, raw):
    checkout_id = uuid.uuid4()
    confirm_id = uuid.uuid4()
    base = {
        "org_id": str(org_id),
        "checkout_id": str(checkout_id),
        "confirm_id": str(confirm_id),
        "external_ref": external_ref,
        "session": "fake_session_mp",
        "customer": "fake_customer_mp",
        "outcome": outcome,
        "event_id": event_id,
        "raw": raw.decode("utf-8"),
    }
    return base


def _evidence_from_spec(spec):
    if spec["outcome"] == "pending":
        return build_pending_evidence(
            organization_id=uuid.UUID(spec["org_id"]),
            confirm_checkout_operation_id=uuid.UUID(spec["confirm_id"]),
            checkout_operation_id=uuid.UUID(spec["checkout_id"]),
            external_operation_ref=spec["external_ref"],
            checkout_session_reference=spec["session"],
            provider_customer_ref=spec["customer"],
            observed_at=datetime.fromtimestamp(1_735_689_600, tz=timezone.utc),
        )
    return build_terminal_evidence(
        organization_id=uuid.UUID(spec["org_id"]),
        confirm_checkout_operation_id=uuid.UUID(spec["confirm_id"]),
        checkout_operation_id=uuid.UUID(spec["checkout_id"]),
        external_operation_ref=spec["external_ref"],
        checkout_session_reference=spec["session"],
        provider_customer_ref=spec["customer"],
        provider_outcome=spec["outcome"],
        provider_event_id=spec["event_id"],
        raw_event=spec["raw"].encode("utf-8"),
        signature_header=f"v1={spec['event_id']}",
        signature_timestamp=1_735_689_600,
    )


def _run_process_race(tmp_path, specs):
    barrier = multiprocessing.Barrier(len(specs))
    queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_multiprocess_worker, args=(str(tmp_path / "provider"), spec, barrier, queue))
        for spec in specs
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
    for process in processes:
        assert process.exitcode == 0
    return [queue.get(timeout=2) for _ in processes]
from tests.platform_billing.test_phase4e2_fake_checkout_simulation import (
    CountingFakeCheckoutOutcomeProducer,
    CountingPayloadStore,
    _clear_simulation_services_override,
    _enable_simulation,
    _simulate,
)


def _ids(org_id: uuid.UUID = ADMIN_ORG_ID):
    checkout_id = uuid.uuid4()
    confirm_id = uuid.uuid4()
    external_ref = f"fake_confirm_{checkout_id.hex}_testref"
    return checkout_id, confirm_id, external_ref


def _terminal_evidence(*, org_id=ADMIN_ORG_ID, outcome="succeeded", raw=b'{"ok":true}', event_id="evt_fake_e2c1"):
    checkout_id, confirm_id, external_ref = _ids(org_id)
    return build_terminal_evidence(
        organization_id=org_id,
        confirm_checkout_operation_id=confirm_id,
        checkout_operation_id=checkout_id,
        external_operation_ref=external_ref,
        checkout_session_reference="fake_session_e2c1",
        provider_customer_ref="fake_customer_e2c1",
        provider_outcome=outcome,
        provider_event_id=event_id,
        raw_event=raw,
        signature_header="v1=test",
        signature_timestamp=1_735_689_600,
    )


def _pending_evidence(*, org_id=ADMIN_ORG_ID):
    checkout_id, confirm_id, external_ref = _ids(org_id)
    return build_pending_evidence(
        organization_id=org_id,
        confirm_checkout_operation_id=confirm_id,
        checkout_operation_id=checkout_id,
        external_operation_ref=external_ref,
        checkout_session_reference="fake_session_e2c1",
        provider_customer_ref="fake_customer_e2c1",
        observed_at=datetime.now(timezone.utc),
    )


async def _confirm_operation(
    db_session: AsyncSession,
    *,
    checkout_operation_id: str | None = None,
    org_id=ADMIN_ORG_ID,
) -> PlatformProviderOperation:
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
    statement = select(PlatformProviderOperation).where(
        PlatformProviderOperation.organization_id == org_id,
        PlatformProviderOperation.operation_type == "confirm_checkout",
    )
    if checkout_operation_id is not None:
        checkout_hex = uuid.UUID(checkout_operation_id).hex
        statement = statement.where(PlatformProviderOperation.external_operation_ref.like(f"fake_confirm_{checkout_hex}_%"))
    return (await db_session.execute(statement)).scalar_one()


async def _ensure_fake_customer(db_session: AsyncSession, org_id=ADMIN_ORG_ID) -> None:
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
            VALUES (:org_id, :name, :slug, 'basic', true, 1, 'USD')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "org_id": str(org_id),
            "name": f"E2C1 Org {org_id.hex[:8]}",
            "slug": f"e2c1-org-{org_id.hex[:8]}",
        },
    )
    await db_session.execute(
        text(
            """
            INSERT INTO platform_provider_customers (
                id, organization_id, provider_code, external_customer_ref, status
            )
            VALUES (:id, :org_id, 'fake', :external_ref, 'active')
            ON CONFLICT (organization_id, provider_code) DO UPDATE
            SET status = 'active'
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "org_id": str(org_id),
            "external_ref": f"cus_fake_e2c1_{org_id.hex[:8]}",
        },
    )
    await db_session.commit()


async def _webhook_inbox_count(db_session: AsyncSession) -> int:
    return len((await db_session.execute(select(PlatformWebhookInbox))).scalars().all())


def _sync_admin_dsn() -> str:
    raw = os.environ.get("TEST_ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL") or settings.TEST_DATABASE_URL or settings.DATABASE_URL
    url = make_url(raw)
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def test_runtime_composition_uses_distinct_durable_provider_evidence_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "provider"))
    runtime = default_simulation_services()
    reader = default_fake_checkout_evidence_reader()

    assert isinstance(runtime.payload_store, LocalEncryptedWebhookPayloadStore)
    assert isinstance(runtime.evidence_store, LocalEncryptedFakeCheckoutEvidenceStore)
    assert isinstance(reader._store, LocalEncryptedFakeCheckoutEvidenceStore)
    assert runtime.evidence_store.root_dir != runtime.payload_store.root_dir
    assert runtime.evidence_store.root_dir == reader._store.root_dir

    test_store = InMemoryFakeCheckoutEvidenceStore()
    services = CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=CountingPayloadStore(),
        evidence_store=test_store,
    )
    assert services.evidence_store is test_store


def test_runtime_composition_fails_closed_for_missing_unusable_or_repo_local_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", "")
    with pytest.raises(FakeCheckoutEvidenceStorageFailure):
        asyncio.run(default_simulation_services().evidence_store.record(_pending_evidence()))

    file_path = tmp_path / "not-a-directory"
    file_path.write_text("occupied")
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(file_path))
    with pytest.raises(FakeCheckoutEvidenceStorageFailure):
        asyncio.run(default_simulation_services().evidence_store.record(_pending_evidence()))

    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(Path.cwd() / ".tmp-provider-evidence"))
    with pytest.raises(FakeCheckoutEvidenceStorageFailure):
        asyncio.run(default_simulation_services().evidence_store.record(_pending_evidence()))


def test_disabled_simulation_does_not_create_provider_evidence_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR", str(tmp_path / "provider"))
    monkeypatch.setattr(settings, "PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED", False)
    services = default_simulation_services()
    assert isinstance(services.evidence_store, LocalEncryptedFakeCheckoutEvidenceStore)
    assert not (tmp_path / "provider").exists()


@pytest.mark.asyncio
async def test_terminal_and_pending_evidence_survive_new_store_instance(tmp_path):
    store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    terminal = await store.record(_terminal_evidence())
    pending = await store.record(_pending_evidence(org_id=OWNER_ORG_ID))

    restarted = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    loaded_terminal = await restarted.get(
        provider_code="fake",
        organization_id=terminal.organization_id,
        external_operation_ref=terminal.external_operation_ref,
    )
    loaded_pending = await restarted.get(
        provider_code="fake",
        organization_id=pending.organization_id,
        external_operation_ref=pending.external_operation_ref,
    )

    assert loaded_terminal == terminal
    assert loaded_terminal.raw_event == b'{"ok":true}'
    assert loaded_pending == pending
    assert loaded_pending.raw_event is None


@pytest.mark.asyncio
async def test_file_store_integrity_permissions_and_private_filenames(tmp_path):
    store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    evidence = await store.record(_terminal_evidence(raw=b"secret-session-data"))
    files = list((tmp_path / "provider").rglob("*.evidence"))

    assert len(files) == 1
    assert evidence.external_operation_ref not in files[0].name
    assert evidence.checkout_session_reference not in files[0].name
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert b"secret-session-data" not in files[0].read_bytes()

    files[0].write_bytes(files[0].read_bytes()[:20])
    with pytest.raises(FakeCheckoutEvidenceCorrupt):
        await store.get(
            provider_code="fake",
            organization_id=evidence.organization_id,
            external_operation_ref=evidence.external_operation_ref,
        )


@pytest.mark.asyncio
async def test_associated_data_mismatch_and_crypto_purpose_separation(tmp_path):
    provider_store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    evidence = await provider_store.record(_terminal_evidence(raw=b'{"provider":true}'))
    evidence_file = next((tmp_path / "provider").rglob("*.evidence"))

    wrong_tenant_path = provider_store._path_for("fake", OWNER_ORG_ID, evidence.external_operation_ref)
    wrong_tenant_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_tenant_path.write_bytes(evidence_file.read_bytes())
    with pytest.raises(FakeCheckoutEvidenceCorrupt):
        await provider_store.get(
            provider_code="fake",
            organization_id=OWNER_ORG_ID,
            external_operation_ref=evidence.external_operation_ref,
        )

    webhook_store = LocalEncryptedWebhookPayloadStore(tmp_path / "webhook")
    webhook_pointer = f"file-encrypted://{evidence_file.resolve()}"
    assert await webhook_store.get_verified_payload(webhook_pointer) != evidence.raw_event

    webhook_payload = await webhook_store.put_verified_payload(
        provider_code="fake",
        provider_event_id="evt_webhook_ciphertext",
        payload_sha256="0" * 64,
        raw_body=b'{"webhook":true}',
    )
    webhook_file = Path(webhook_payload.encrypted_payload_ref.removeprefix("file-encrypted://"))
    provider_path = provider_store._path_for("fake", ADMIN_ORG_ID, "fake_confirm_cross_ciphertext")
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_bytes(webhook_file.read_bytes())
    with pytest.raises(FakeCheckoutEvidenceCorrupt):
        await provider_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref="fake_confirm_cross_ciphertext",
        )


@pytest.mark.asyncio
async def test_failed_atomic_write_leaves_no_authoritative_record(tmp_path, monkeypatch):
    store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    evidence = _terminal_evidence()

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(FakeCheckoutEvidenceStorageFailure):
        await store.record(evidence)

    assert list((tmp_path / "provider").rglob("*.evidence")) == []
    assert list((tmp_path / "provider").rglob("*.tmp")) == []


def test_multiprocess_same_terminal_race_converges_idempotently(tmp_path):
    external_ref = f"fake_confirm_{uuid.uuid4().hex}_mp"
    spec = _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="succeeded", event_id="evt_same_mp", raw=b"same")
    results = _run_process_race(tmp_path, [spec, spec])

    assert all(result["ok"] for result in results)
    assert len({result["event_id"] for result in results}) == 1
    assert len({result["evidence_hash"] for result in results}) == 1
    assert len(list((tmp_path / "provider").rglob("*.evidence"))) == 1
    assert list((tmp_path / "provider").rglob("*.tmp")) == []


def test_multiprocess_opposite_terminal_race_preserves_one_winner(tmp_path):
    external_ref = f"fake_confirm_{uuid.uuid4().hex}_mp"
    specs = [
        _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="succeeded", event_id="evt_succeeded_mp", raw=b"succeeded"),
        _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="failed", event_id="evt_failed_mp", raw=b"failed"),
    ]
    results = _run_process_race(tmp_path, specs)

    assert sum(1 for result in results if result["ok"]) == 1
    assert sum(1 for result in results if not result["ok"] and result["error"] == "FakeCheckoutEvidenceConflict") == 1
    stored = asyncio.run(
        LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider").get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=external_ref,
        )
    )
    assert stored.provider_outcome in {"succeeded", "failed"}
    assert stored.provider_event_id in {"evt_succeeded_mp", "evt_failed_mp"}
    assert len(list((tmp_path / "provider").rglob("*.evidence"))) == 1
    assert list((tmp_path / "provider").rglob("*.tmp")) == []


def test_multiprocess_pending_terminal_race_is_legal_and_terminal_preserved(tmp_path):
    external_ref = f"fake_confirm_{uuid.uuid4().hex}_mp"
    specs = [
        _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="pending", event_id="evt_pending_mp", raw=b"pending"),
        _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="succeeded", event_id="evt_terminal_mp", raw=b"terminal"),
    ]
    results = _run_process_race(tmp_path, specs)

    assert any(result["ok"] for result in results)
    stored = asyncio.run(
        LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider").get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=external_ref,
        )
    )
    assert stored.provider_outcome == "succeeded"
    assert stored.provider_event_id == "evt_terminal_mp"
    assert all(result["ok"] or result["error"] == "FakeCheckoutEvidenceConflict" for result in results)


def test_multiprocess_cross_tenant_collision_is_isolated(tmp_path):
    external_ref = f"fake_confirm_{uuid.uuid4().hex}_shared"
    specs = [
        _evidence_spec(org_id=ADMIN_ORG_ID, external_ref=external_ref, outcome="succeeded", event_id="evt_admin_mp", raw=b"admin"),
        _evidence_spec(org_id=OWNER_ORG_ID, external_ref=external_ref, outcome="failed", event_id="evt_owner_mp", raw=b"owner"),
    ]
    results = _run_process_race(tmp_path, specs)

    assert all(result["ok"] for result in results)
    store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    admin = asyncio.run(store.get(provider_code="fake", organization_id=ADMIN_ORG_ID, external_operation_ref=external_ref))
    owner = asyncio.run(store.get(provider_code="fake", organization_id=OWNER_ORG_ID, external_operation_ref=external_ref))
    assert admin.provider_event_id == "evt_admin_mp"
    assert owner.provider_event_id == "evt_owner_mp"
    assert len(list((tmp_path / "provider").rglob("*.evidence"))) == 2


@pytest.mark.asyncio
async def test_transition_matrix_and_conflicts_preserve_original():
    store = InMemoryFakeCheckoutEvidenceStore()
    pending = await store.record(_pending_evidence())
    assert (await store.record(pending)).canonical_evidence_hash == pending.canonical_evidence_hash

    terminal = build_terminal_evidence(
        organization_id=pending.organization_id,
        confirm_checkout_operation_id=pending.confirm_checkout_operation_id,
        checkout_operation_id=pending.checkout_operation_id,
        external_operation_ref=pending.external_operation_ref,
        checkout_session_reference=pending.checkout_session_reference,
        provider_customer_ref=pending.provider_customer_ref,
        provider_outcome="succeeded",
        provider_event_id="evt_terminal",
        raw_event=b"terminal",
        signature_header="v1=terminal",
        signature_timestamp=1_735_689_600,
    )
    stored = await store.record(terminal)
    assert stored.provider_outcome == "succeeded"
    assert (await store.record(terminal)).canonical_evidence_hash == stored.canonical_evidence_hash

    opposite = build_terminal_evidence(
        organization_id=stored.organization_id,
        confirm_checkout_operation_id=stored.confirm_checkout_operation_id,
        checkout_operation_id=stored.checkout_operation_id,
        external_operation_ref=stored.external_operation_ref,
        checkout_session_reference=stored.checkout_session_reference,
        provider_customer_ref=stored.provider_customer_ref,
        provider_outcome="failed",
        provider_event_id="evt_failed",
        raw_event=b"failed",
        signature_header="v1=failed",
        signature_timestamp=1_735_689_601,
    )
    with pytest.raises(FakeCheckoutEvidenceConflict):
        await store.record(opposite)
    assert (await store.get(provider_code="fake", organization_id=stored.organization_id, external_operation_ref=stored.external_operation_ref)).provider_outcome == "succeeded"


@pytest.mark.asyncio
async def test_none_to_failed_and_same_event_different_hash_conflict():
    store = InMemoryFakeCheckoutEvidenceStore()
    failed = await store.record(_terminal_evidence(outcome="failed", raw=b"failed", event_id="evt_same"))
    assert failed.provider_outcome == "failed"

    changed = build_terminal_evidence(
        organization_id=failed.organization_id,
        confirm_checkout_operation_id=failed.confirm_checkout_operation_id,
        checkout_operation_id=failed.checkout_operation_id,
        external_operation_ref=failed.external_operation_ref,
        checkout_session_reference=failed.checkout_session_reference,
        provider_customer_ref=failed.provider_customer_ref,
        provider_outcome="failed",
        provider_event_id="evt_same",
        raw_event=b"different",
        signature_header="v1=different",
        signature_timestamp=1_735_689_602,
    )
    with pytest.raises(FakeCheckoutEvidenceConflict):
        await store.record(changed)


@pytest.mark.asyncio
async def test_reader_returns_provider_neutral_evidence_and_blocks_cross_tenant():
    store = InMemoryFakeCheckoutEvidenceStore()
    evidence = await store.record(_terminal_evidence())
    reader = LocalFakeCheckoutProviderEvidenceReader(store)

    returned = await reader.fetch_operation_evidence(
        f"fake-provider-evidence:v1:fake:{evidence.organization_id}:{evidence.external_operation_ref}"
    )

    assert returned.provider_status == "succeeded"
    assert returned.external_operation_ref == evidence.external_operation_ref
    assert returned.evidence_sha256 == evidence.canonical_evidence_hash
    assert await store.get(provider_code="fake", organization_id=OWNER_ORG_ID, external_operation_ref=evidence.external_operation_ref) is None


@pytest.mark.asyncio
async def test_e2b_records_provider_evidence_before_webhook_delivery(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = InMemoryFakeCheckoutEvidenceStore()
    payload_store = CountingPayloadStore()
    services = CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=payload_store,
        evidence_store=evidence_store,
    )
    app.dependency_overrides[default_simulation_services] = lambda: services
    try:
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-checkout-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()

        response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

        assert response.status_code == 200, response.text
        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(
            provider_code="fake",
            organization_id=ADMIN_ORG_ID,
            external_operation_ref=operation.external_operation_ref,
        )
        assert evidence is not None
        assert evidence.provider_outcome == "succeeded"
        assert evidence.raw_event is not None
        assert payload_store.write_count == 1
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_direct_skipped_delivery_records_provider_truth_without_receiver_side_effects(db_session: AsyncSession):
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    checkout_id = uuid.uuid4()
    confirm_id = uuid.uuid4()
    external_ref = f"fake_confirm_{checkout_id.hex}_direct_skip"
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(ADMIN_ORG_ID)},
    )
    db_session.add(
        PlatformProviderOperation(
            id=confirm_id,
            organization_id=ADMIN_ORG_ID,
            provider_code="fake",
            operation_type="confirm_checkout",
            idempotency_key=f"direct-skip-{uuid.uuid4().hex[:12]}",
            canonical_request_sha256="0" * 64,
            status="in_progress",
            external_operation_ref=external_ref,
            attempt_count=1,
        )
    )
    await db_session.commit()
    before_inbox = await _webhook_inbox_count(db_session)
    store = InMemoryFakeCheckoutEvidenceStore()
    evidence = await store.record(
        build_terminal_evidence(
            organization_id=ADMIN_ORG_ID,
            confirm_checkout_operation_id=confirm_id,
            checkout_operation_id=checkout_id,
            external_operation_ref=external_ref,
            checkout_session_reference="fake_session_direct_skip",
            provider_customer_ref="fake_customer_direct_skip",
            provider_outcome="succeeded",
            provider_event_id="evt_direct_skip",
            raw_event=b"direct-skip",
            signature_header="v1=direct-skip",
            signature_timestamp=1_735_689_600,
        )
    )
    reader = LocalFakeCheckoutProviderEvidenceReader(store)
    returned = await reader.fetch_operation_evidence(
        f"fake-provider-evidence:v1:fake:{ADMIN_ORG_ID}:{external_ref}"
    )
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(ADMIN_ORG_ID)},
    )
    operation = (
        await db_session.execute(
            select(PlatformProviderOperation).where(PlatformProviderOperation.id == confirm_id)
        )
    ).scalar_one()

    assert evidence.provider_outcome == "succeeded"
    assert returned.provider_status == "succeeded"
    assert await _webhook_inbox_count(db_session) == before_inbox
    assert operation.status == "in_progress"


@pytest.mark.asyncio
async def test_provider_evidence_saved_when_webhook_delivery_is_skipped(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = InMemoryFakeCheckoutEvidenceStore()

    async def fail_accept(self, envelope):
        raise RuntimeError("delivery skipped")

    monkeypatch.setattr(PlatformWebhookAcceptanceService, "accept", fail_accept)
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=CountingPayloadStore(),
        evidence_store=evidence_store,
    )
    try:
        before_inbox = await _webhook_inbox_count(db_session)
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-lost-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()

        with pytest.raises(RuntimeError):
            await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(provider_code="fake", organization_id=ADMIN_ORG_ID, external_operation_ref=operation.external_operation_ref)
        assert evidence is not None
        assert evidence.provider_outcome == "succeeded"
        assert operation.status == "in_progress"
        assert await _webhook_inbox_count(db_session) == before_inbox
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_receiver_payload_store_failure_preserves_provider_truth_and_retry_reuses_event(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    class FailingOncePayloadStore(CountingPayloadStore):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def put_verified_payload(self, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("receiver payload failed")
            return await super().put_verified_payload(**kwargs)

    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = InMemoryFakeCheckoutEvidenceStore()
    producer = CountingFakeCheckoutOutcomeProducer()
    payload_store = FailingOncePayloadStore()
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=producer,
        payload_store=payload_store,
        evidence_store=evidence_store,
    )
    try:
        before_inbox = await _webhook_inbox_count(db_session)
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-payload-fail-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()
        key = f"e2c1-payload-fail-key-{uuid.uuid4().hex[:8]}"

        with pytest.raises(RuntimeError):
            await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
        first_event = producer.events[0]
        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(provider_code="fake", organization_id=ADMIN_ORG_ID, external_operation_ref=operation.external_operation_ref)
        assert evidence.provider_event_id == first_event.provider_event_id
        assert evidence.raw_event_sha256 == first_event.raw_body.hex() or evidence.raw_event == first_event.raw_body
        assert operation.status == "in_progress"
        assert await _webhook_inbox_count(db_session) == before_inbox

        response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
        assert response.status_code == 200
        assert producer.generate_calls == 1
        assert producer.events[0].raw_body == first_event.raw_body
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_inbox_insertion_failure_preserves_provider_truth_and_receiver_compensation(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = InMemoryFakeCheckoutEvidenceStore()
    payload_store = CountingPayloadStore()

    async def fail_accept(self, **kwargs):
        raise SQLAlchemyError("inbox insert failed")

    monkeypatch.setattr(PlatformWebhookInboxRepository, "accept", fail_accept)
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=payload_store,
        evidence_store=evidence_store,
    )
    try:
        before_inbox = await _webhook_inbox_count(db_session)
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-inbox-fail-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()

        with pytest.raises(Exception):
            await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        evidence = await evidence_store.get(provider_code="fake", organization_id=ADMIN_ORG_ID, external_operation_ref=operation.external_operation_ref)
        assert evidence is not None
        assert evidence.provider_outcome == "succeeded"
        assert payload_store.delete_calls == payload_store.put_calls
        assert operation.status == "in_progress"
        assert await _webhook_inbox_count(db_session) == before_inbox
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_evidence_write_failure_prevents_phase4c_delivery(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    class FailingEvidenceStore(InMemoryFakeCheckoutEvidenceStore):
        async def record(self, evidence):
            raise FakeCheckoutEvidenceStorageFailure("boom")

    class ObservedPayloadStore(CountingPayloadStore):
        pass

    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    payload_store = ObservedPayloadStore()
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=payload_store,
        evidence_store=FailingEvidenceStore(),
    )
    try:
        before_inbox = await _webhook_inbox_count(db_session)
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-fail-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()

        with pytest.raises(FakeCheckoutEvidenceStorageFailure):
            await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

        operation = await _confirm_operation(db_session, checkout_operation_id=checkout["operation_id"])
        assert operation.status == "in_progress"
        assert payload_store.write_count == 0
        assert await _webhook_inbox_count(db_session) == before_inbox
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_transaction_boundary_reservation_visible_and_unlocked_before_evidence_write(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    class ProbingEvidenceStore(InMemoryFakeCheckoutEvidenceStore):
        def __init__(self):
            super().__init__()
            self.probed = False
            self.accept_started_after_record = False
            self.record_returned = False

        async def record(self, evidence):
            with psycopg2.connect(_sync_admin_dsn()) as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(evidence.organization_id),))
                    cur.execute(
                        "SELECT status FROM platform_provider_operations WHERE id = %s FOR UPDATE NOWAIT",
                        (str(evidence.confirm_checkout_operation_id),),
                    )
                    assert cur.fetchone()[0] == "in_progress"
                conn.rollback()
            self.probed = True
            stored = await super().record(evidence)
            self.record_returned = True
            return stored

    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = ProbingEvidenceStore()
    original_accept = PlatformWebhookAcceptanceService.accept

    async def observed_accept(self, envelope):
        assert evidence_store.record_returned is True
        evidence_store.accept_started_after_record = True
        return await original_accept(self, envelope)

    monkeypatch.setattr(PlatformWebhookAcceptanceService, "accept", observed_accept)
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=CountingPayloadStore(),
        evidence_store=evidence_store,
    )
    try:
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-tx-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()
        response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")
        assert response.status_code == 200
        assert evidence_store.probed is True
        assert evidence_store.accept_started_after_record is True
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_retry_reuses_original_provider_event_after_receiver_failure(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    evidence_store = InMemoryFakeCheckoutEvidenceStore()
    producer = CountingFakeCheckoutOutcomeProducer()
    calls = {"count": 0}
    original_accept = PlatformWebhookAcceptanceService.accept

    async def fail_once(self, envelope):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("first delivery lost")
        return await original_accept(self, envelope)

    monkeypatch.setattr(PlatformWebhookAcceptanceService, "accept", fail_once)
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=producer,
        payload_store=CountingPayloadStore(),
        evidence_store=evidence_store,
    )
    try:
        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-retry-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()
        key = f"e2c1-retry-key-{uuid.uuid4().hex[:8]}"

        with pytest.raises(RuntimeError):
            await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)
        first_event = producer.events[0]

        response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded", key=key)

        assert response.status_code == 200
        assert producer.generate_calls == 1
        assert producer.events[0].provider_event_id == first_event.provider_event_id
        assert producer.events[0].raw_body == first_event.raw_body
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_provider_outcome_does_not_mutate_subscription_entitlement_projection_or_payment_tables(
    client: AsyncClient,
    admin_token_headers,
    monkeypatch,
    platform_catalog,
    db_session: AsyncSession,
):
    _enable_simulation(monkeypatch)
    await _ensure_fake_customer(db_session, org_id=ADMIN_ORG_ID)
    app.dependency_overrides[default_simulation_services] = lambda: CheckoutSimulationServices(
        event_producer=CountingFakeCheckoutOutcomeProducer(),
        payload_store=CountingPayloadStore(),
        evidence_store=InMemoryFakeCheckoutEvidenceStore(),
    )
    try:
        before = {}
        for table in ["platform_subscriptions", "platform_entitlement_projection", "platform_access_projection"]:
            before[table] = await db_session.scalar(text(f"SELECT count(*) FROM {table}"))
        forbidden = await db_session.execute(
            text(
                """
                SELECT relname
                FROM pg_class
                WHERE relname IN (
                  'platform_payments', 'platform_invoices', 'platform_refunds',
                  'platform_ledger_entries'
                )
                ORDER BY relname
                """
            )
        )
        assert forbidden.scalars().all() == []

        checkout = (await _post_checkout(client, admin_token_headers, idempotency_key=f"e2c1-safety-{uuid.uuid4().hex[:12]}", plan_code=platform_catalog["plan_code"])).json()
        response = await _simulate(client, admin_token_headers, checkout_operation_id=checkout["operation_id"], outcome="succeeded")

        assert response.status_code == 200
        body = response.json()
        assert body["subscription_activated"] is False
        assert body["browser_authoritative"] is False
        assert "payment_id" not in body
        assert "invoice_id" not in body
        assert "refund_id" not in body
        for table, count in before.items():
            assert await db_session.scalar(text(f"SELECT count(*) FROM {table}")) == count
    finally:
        _clear_simulation_services_override()


@pytest.mark.asyncio
async def test_process_restart_before_delivery_loads_exact_original_event(tmp_path):
    store = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    original = _terminal_evidence(raw=b"restart-event", event_id="evt_restart")
    stored = await store.record(original)
    del store

    restarted = LocalEncryptedFakeCheckoutEvidenceStore(tmp_path / "provider")
    loaded = await restarted.get(
        provider_code="fake",
        organization_id=stored.organization_id,
        external_operation_ref=stored.external_operation_ref,
    )

    assert loaded.provider_event_id == original.provider_event_id
    assert loaded.provider_observed_at == original.provider_observed_at
    assert loaded.raw_event == original.raw_event
    assert loaded.raw_event_sha256 == original.raw_event_sha256
    assert loaded.signature_header == original.signature_header
    assert loaded.provider_outcome == original.provider_outcome


@pytest.mark.asyncio
async def test_stale_original_webhook_is_not_timestamp_refreshed(monkeypatch):
    producer = CountingFakeCheckoutOutcomeProducer(clock=lambda: datetime.fromtimestamp(1_600_000_000, tz=timezone.utc))
    evidence_store = InMemoryFakeCheckoutEvidenceStore()
    payload_store = CountingPayloadStore()
    checkout_id, confirm_id, external_ref = _ids()
    event = producer.generate(
        organization_id=ADMIN_ORG_ID,
        checkout_operation_id=checkout_id,
        checkout_session_reference="fake_session_stale",
        simulation_operation_id=confirm_id,
        external_operation_ref=external_ref,
        provider_customer_ref="fake_customer_stale",
        requested_outcome="succeeded",
    )
    evidence = await evidence_store.record(
        build_terminal_evidence(
            organization_id=ADMIN_ORG_ID,
            confirm_checkout_operation_id=confirm_id,
            checkout_operation_id=checkout_id,
            external_operation_ref=external_ref,
            checkout_session_reference="fake_session_stale",
            provider_customer_ref="fake_customer_stale",
            provider_outcome="succeeded",
            provider_event_id=event.provider_event_id,
            raw_event=event.raw_body,
            signature_header=event.signature,
            signature_timestamp=event.event_timestamp,
        )
    )
    assert evidence.signature_timestamp == 1_600_000_000
    assert producer.generate_calls == 1


@pytest.mark.asyncio
async def test_concurrent_same_outcome_and_opposite_terminal_are_deterministic():
    for _ in range(20):
        store = InMemoryFakeCheckoutEvidenceStore()
        evidence = _terminal_evidence(event_id=f"evt_{uuid.uuid4().hex}", raw=b"same")
        same = await asyncio.gather(store.record(evidence), store.record(evidence))
        assert same[0].canonical_evidence_hash == same[1].canonical_evidence_hash

        opposite = build_terminal_evidence(
            organization_id=evidence.organization_id,
            confirm_checkout_operation_id=evidence.confirm_checkout_operation_id,
            checkout_operation_id=evidence.checkout_operation_id,
            external_operation_ref=evidence.external_operation_ref,
            checkout_session_reference=evidence.checkout_session_reference,
            provider_customer_ref=evidence.provider_customer_ref,
            provider_outcome="failed",
            provider_event_id=f"evt_fail_{uuid.uuid4().hex}",
            raw_event=b"failed",
            signature_header="v1=failed",
            signature_timestamp=1_735_689_604,
        )
        with pytest.raises(FakeCheckoutEvidenceConflict):
            await store.record(opposite)
