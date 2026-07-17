from __future__ import annotations

import asyncio
import math
import uuid

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.domain.synthetic_organizations import (
    SYNTHETIC_ORGANIZATION_DESCRIPTION,
    SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
    SyntheticOrganizationCreationCommand,
    SyntheticOrganizationError,
    SyntheticOrganizationLockContentionError,
)
from app.repositories.synthetic_organizations import (
    SYNTHETIC_ORGANIZATION_LOCK_POLL_INTERVAL_SECONDS,
    SYNTHETIC_ORGANIZATION_LOCK_TIMEOUT_SECONDS,
    SyntheticOrganizationRepository,
    synthetic_organization_advisory_lock_key,
)
from app.services.synthetic_organizations import SyntheticOrganizationCreationService


NAME = "Vitara TEST Razorpay Smoke Org"
SLUG = "vitara-test-razorpay-smoke-org"
KEY = "organization-create:synthetic:test:phase6an-d10"


class _NoDbSession:
    def __init__(self):
        self.touched = False

    async def execute(self, *args, **kwargs):
        self.touched = True
        raise AssertionError("database should not be touched during timing validation")


async def _unexpected_sleep(delay: float) -> None:
    raise AssertionError("sleep should not be reached during timing validation")


def command(**overrides) -> SyntheticOrganizationCreationCommand:
    values = {
        "name": NAME,
        "slug": SLUG,
        "idempotency_key": KEY,
        "synthetic_mode": True,
        "trusted_source": SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
    }
    values.update(overrides)
    return SyntheticOrganizationCreationCommand(**values)


async def count_scalar(session, sql: str, params: dict[str, object] | None = None) -> int:
    if params and params.get("id") is not None:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(params["id"])},
        )
    result = await session.execute(text(sql), params or {})
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_valid_synthetic_organization_creation_is_sanitized_and_isolated():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        result = await service.create_synthetic_organization(command())

        row = (await session.execute(text("SELECT * FROM organizations WHERE id = :id"), {"id": result.organization_id})).mappings().one()
        assert row["name"] == NAME
        assert row["slug"] == SLUG
        assert row["tier"] == "basic"
        assert row["is_active"] is True
        assert row["max_branches"] == 1
        assert row["default_currency_code"] == "INR"
        assert row["business_type"] == "synthetic_test"
        assert row["description"] == SYNTHETIC_ORGANIZATION_DESCRIPTION
        assert result.replayed is False
        assert result.currency == "INR"
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency") == 1
        assert await count_scalar(session, "SELECT count(*) FROM owners WHERE org_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM gyms WHERE org_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM organization_users WHERE org_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM organization_members WHERE org_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM org_branches WHERE org_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM finance.billing_parties WHERE organization_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM finance.invoices WHERE organization_id = :id", {"id": result.organization_id}) == 0
        assert await count_scalar(session, "SELECT count(*) FROM finance.payments WHERE organization_id = :id", {"id": result.organization_id}) == 0
        await tx.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "environment", "code"),
    [
        ({}, "production", "SYNTHETIC_ORG_PRODUCTION_REJECTED"),
        ({}, "unknown", "SYNTHETIC_ORG_ENVIRONMENT_REJECTED"),
        ({}, "", "SYNTHETIC_ORG_ENVIRONMENT_REJECTED"),
        ({"synthetic_mode": False}, "development", "SYNTHETIC_ORG_MODE_REQUIRED"),
        ({"trusted_source": "browser"}, "development", "SYNTHETIC_ORG_SOURCE_REJECTED"),
        ({"name": "Customer Fitness"}, "development", "SYNTHETIC_ORG_NAME_UNSAFE"),
        ({"slug": "customer-fitness"}, "development", "SYNTHETIC_ORG_SLUG_UNSAFE"),
        ({"slug": "bad slug"}, "development", "SYNTHETIC_ORG_SLUG_INVALID"),
        ({"idempotency_key": "bad-key"}, "development", "SYNTHETIC_ORG_IDEMPOTENCY_INVALID"),
        ({"name": "TEST\x00Org"}, "development", "SYNTHETIC_ORG_NAME_INVALID"),
    ],
)
async def test_validation_rejects_unsafe_commands(overrides, environment, code):
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment=environment)
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command(**overrides))
        assert exc.value.code == code
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug LIKE 'vitara-test-razorpay-smoke-org%'") == 0
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency") == 0
        await tx.rollback()


@pytest.mark.asyncio
async def test_same_key_replays_and_changed_payload_conflicts_without_mutation():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="test")
        created = await service.create_synthetic_organization(command(name="  Vitara   TEST Razorpay Smoke Org  ", slug=SLUG.upper()))
        replayed = await service.create_synthetic_organization(command(name=NAME, slug=SLUG))
        assert replayed.organization_id == created.organization_id
        assert replayed.replayed is True
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": SLUG}) == 1
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency") == 1
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command(name="Vitara SANDBOX Changed Org"))
        assert exc.value.code == "SYNTHETIC_ORG_IDEMPOTENCY_CONFLICT"
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": SLUG}) == 1
        await tx.rollback()


@pytest.mark.asyncio
async def test_different_key_same_identity_conflicts_without_second_mapping():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        await service.create_synthetic_organization(command())
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command(idempotency_key="organization-create:synthetic:test:phase6an-d10-other"))
        assert exc.value.code == "SYNTHETIC_ORG_DUPLICATE_IDENTITY"
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": SLUG}) == 1
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency") == 1
        await tx.rollback()


@pytest.mark.asyncio
async def test_replay_integrity_rejects_drift_and_inactive_state():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        created = await service.create_synthetic_organization(command())
        await session.execute(text("UPDATE organizations SET description = 'drifted' WHERE id = :id"), {"id": created.organization_id})
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command())
        assert exc.value.code == "SYNTHETIC_ORG_REPLAY_INTEGRITY_CONFLICT"
        await session.execute(text("UPDATE organizations SET description = :description, is_active = false WHERE id = :id"), {"description": SYNTHETIC_ORGANIZATION_DESCRIPTION, "id": created.organization_id})
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command())
        assert exc.value.code == "SYNTHETIC_ORG_REPLAY_INTEGRITY_CONFLICT"
        await tx.rollback()


@pytest.mark.asyncio
async def test_unsupported_canonicalization_version_is_sanitized():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        await service.create_synthetic_organization(command())
        await session.execute(text("ALTER TABLE organization_creation_idempotency DISABLE TRIGGER trg_organization_creation_idempotency_immutable"))
        await session.execute(text("UPDATE organization_creation_idempotency SET canonicalization_version = 2"))
        await session.execute(text("ALTER TABLE organization_creation_idempotency ENABLE TRIGGER trg_organization_creation_idempotency_immutable"))
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command())
        assert exc.value.code == "SYNTHETIC_ORG_UNSUPPORTED_CANONICAL_VERSION"
        await tx.rollback()


@pytest.mark.asyncio
async def test_caller_rollback_after_success_removes_org_and_evidence():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        result = await service.create_synthetic_organization(command(slug="vitara-test-rollback-org", idempotency_key="organization-create:synthetic:test:rollback"))
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE id = :id", {"id": result.organization_id}) == 1
        await tx.rollback()

    async with AsyncSessionLocal() as session:
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug = 'vitara-test-rollback-org'") == 0
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = 'organization-create:synthetic:test:rollback'") == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_and_different_key_same_slug_are_bounded():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        service = SyntheticOrganizationCreationService(session, environment="development")
        first = await service.create_synthetic_organization(command(slug="vitara-test-concurrent-org", idempotency_key="organization-create:synthetic:test:concurrent"))
        replay = await service.create_synthetic_organization(command(slug="vitara-test-concurrent-org", idempotency_key="organization-create:synthetic:test:concurrent"))
        assert replay.organization_id == first.organization_id
        assert replay.replayed is True
        with pytest.raises(SyntheticOrganizationError) as exc:
            await service.create_synthetic_organization(command(slug="vitara-test-concurrent-org", idempotency_key="organization-create:synthetic:test:concurrent-other"))
        assert exc.value.code == "SYNTHETIC_ORG_DUPLICATE_IDENTITY"
        assert await count_scalar(session, "SELECT count(*) FROM organizations WHERE slug = 'vitara-test-concurrent-org'") == 1
        assert await count_scalar(session, "SELECT count(*) FROM organization_creation_idempotency") == 1
        await tx.rollback()


async def cleanup_committed_synthetic(slug: str, keys: list[str]) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("ALTER TABLE organization_creation_idempotency DISABLE TRIGGER trg_organization_creation_idempotency_immutable"))
            for key in keys:
                await session.execute(text("DELETE FROM organization_creation_idempotency WHERE idempotency_key = :key"), {"key": key})
            await session.execute(text("ALTER TABLE organization_creation_idempotency ENABLE TRIGGER trg_organization_creation_idempotency_immutable"))
            await session.execute(text("DELETE FROM organizations WHERE slug = :slug"), {"slug": slug})


async def committed_count(sql: str, params: dict[str, object] | None = None) -> int:
    async with AsyncSessionLocal() as session:
        return await count_scalar(session, sql, params)


def contended_sleep_marker(event: asyncio.Event):
    async def _sleep(delay: float) -> None:
        event.set()
        await asyncio.sleep(delay)

    return _sleep


@pytest.mark.asyncio
async def test_real_concurrent_same_key_same_payload_waits_then_replays():
    slug = "vitara-test-r3-same-key-replay"
    key = "organization-create:synthetic:test:r3-same-key-replay"
    waited = asyncio.Event()

    try:
        async with AsyncSessionLocal() as session_a:
            tx_a = await session_a.begin()
            service_a = SyntheticOrganizationCreationService(session_a, environment="development")
            created = await service_a.create_synthetic_organization(command(slug=slug, idempotency_key=key))

            async with AsyncSessionLocal() as session_b:
                tx_b = await session_b.begin()
                service_b = SyntheticOrganizationCreationService(
                    session_b,
                    environment="development",
                    lock_timeout_seconds=2.0,
                    lock_poll_interval_seconds=0.02,
                    lock_sleep=contended_sleep_marker(waited),
                )
                task = asyncio.create_task(service_b.create_synthetic_organization(command(slug=slug, idempotency_key=key)))
                await asyncio.wait_for(waited.wait(), timeout=1.0)
                assert not task.done()

                await tx_a.commit()
                replayed = await asyncio.wait_for(task, timeout=2.0)
                assert replayed.organization_id == created.organization_id
                assert replayed.replayed is True
                await tx_b.rollback()

        assert await committed_count("SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": slug}) == 1
        assert await committed_count("SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = :key", {"key": key}) == 1
    finally:
        await cleanup_committed_synthetic(slug, [key])


@pytest.mark.asyncio
async def test_real_concurrent_same_key_changed_payload_waits_then_conflicts():
    slug = "vitara-test-r3-same-key-conflict"
    key = "organization-create:synthetic:test:r3-same-key-conflict"
    waited = asyncio.Event()

    try:
        async with AsyncSessionLocal() as session_a:
            tx_a = await session_a.begin()
            service_a = SyntheticOrganizationCreationService(session_a, environment="development")
            await service_a.create_synthetic_organization(command(slug=slug, idempotency_key=key))

            async with AsyncSessionLocal() as session_b:
                tx_b = await session_b.begin()
                service_b = SyntheticOrganizationCreationService(
                    session_b,
                    environment="development",
                    lock_timeout_seconds=2.0,
                    lock_poll_interval_seconds=0.02,
                    lock_sleep=contended_sleep_marker(waited),
                )
                task = asyncio.create_task(
                    service_b.create_synthetic_organization(
                        command(name="Vitara SANDBOX R3 Changed Payload", slug=slug, idempotency_key=key)
                    )
                )
                await asyncio.wait_for(waited.wait(), timeout=1.0)
                assert not task.done()

                await tx_a.commit()
                with pytest.raises(SyntheticOrganizationError) as exc:
                    await asyncio.wait_for(task, timeout=2.0)
                assert exc.value.code == "SYNTHETIC_ORG_IDEMPOTENCY_CONFLICT"
                await tx_b.rollback()

        assert await committed_count("SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": slug}) == 1
        assert await committed_count("SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = :key", {"key": key}) == 1
    finally:
        await cleanup_committed_synthetic(slug, [key])


@pytest.mark.asyncio
async def test_real_concurrent_different_key_same_slug_waits_then_duplicate_conflict():
    slug = "vitara-test-r3-same-slug-conflict"
    key_a = "organization-create:synthetic:test:r3-same-slug-a"
    key_b = "organization-create:synthetic:test:r3-same-slug-b"
    waited = asyncio.Event()

    try:
        async with AsyncSessionLocal() as session_a:
            tx_a = await session_a.begin()
            service_a = SyntheticOrganizationCreationService(session_a, environment="development")
            await service_a.create_synthetic_organization(command(slug=slug, idempotency_key=key_a))

            async with AsyncSessionLocal() as session_b:
                tx_b = await session_b.begin()
                service_b = SyntheticOrganizationCreationService(
                    session_b,
                    environment="development",
                    lock_timeout_seconds=2.0,
                    lock_poll_interval_seconds=0.02,
                    lock_sleep=contended_sleep_marker(waited),
                )
                task = asyncio.create_task(service_b.create_synthetic_organization(command(slug=slug, idempotency_key=key_b)))
                await asyncio.wait_for(waited.wait(), timeout=1.0)
                assert not task.done()

                await tx_a.commit()
                with pytest.raises(SyntheticOrganizationError) as exc:
                    await asyncio.wait_for(task, timeout=2.0)
                assert exc.value.code == "SYNTHETIC_ORG_DUPLICATE_IDENTITY"
                await tx_b.rollback()

        assert await committed_count("SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": slug}) == 1
        assert await committed_count("SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = :key", {"key": key_a}) == 1
        assert await committed_count("SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = :key", {"key": key_b}) == 0
    finally:
        await cleanup_committed_synthetic(slug, [key_a, key_b])


@pytest.mark.asyncio
async def test_bounded_lock_timeout_is_sanitized_and_leaves_caller_transaction_manageable():
    slug = "vitara-test-r3-timeout"
    key = "organization-create:synthetic:test:r3-timeout"
    lock_key = synthetic_organization_advisory_lock_key(f"org-create:idempotency:{key}")

    try:
        async with AsyncSessionLocal() as blocker:
            blocker_tx = await blocker.begin()
            await blocker.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

            async with AsyncSessionLocal() as session_b:
                tx_b = await session_b.begin()
                service_b = SyntheticOrganizationCreationService(
                    session_b,
                    environment="development",
                    lock_timeout_seconds=0.05,
                    lock_poll_interval_seconds=0.01,
                )
                started = asyncio.get_running_loop().time()
                with pytest.raises(SyntheticOrganizationLockContentionError) as exc:
                    await service_b.create_synthetic_organization(command(slug=slug, idempotency_key=key))
                elapsed = asyncio.get_running_loop().time() - started
                assert exc.value.code == "SYNTHETIC_ORG_LOCK_CONTENTION"
                assert elapsed < 1.0
                assert await count_scalar(session_b, "SELECT 1") == 1
                await tx_b.rollback()

            await blocker_tx.rollback()

        assert await committed_count("SELECT count(*) FROM organizations WHERE slug = :slug", {"slug": slug}) == 0
        assert await committed_count("SELECT count(*) FROM organization_creation_idempotency WHERE idempotency_key = :key", {"key": key}) == 0

        async with AsyncSessionLocal() as session:
            tx = await session.begin()
            service = SyntheticOrganizationCreationService(session, environment="development")
            result = await service.create_synthetic_organization(command(slug=slug, idempotency_key=key))
            await tx.commit()
        assert await committed_count("SELECT count(*) FROM organizations WHERE id = :id", {"id": result.organization_id}) == 1
    finally:
        await cleanup_committed_synthetic(slug, [key])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("lock_timeout_seconds", float("nan"), id="timeout-nan"),
        pytest.param("lock_timeout_seconds", float("inf"), id="timeout-pos-inf"),
        pytest.param("lock_timeout_seconds", float("-inf"), id="timeout-neg-inf"),
        pytest.param("lock_timeout_seconds", 0, id="timeout-zero-int"),
        pytest.param("lock_timeout_seconds", 0.0, id="timeout-zero-float"),
        pytest.param("lock_timeout_seconds", -0.01, id="timeout-negative"),
        pytest.param("lock_timeout_seconds", 30.1, id="timeout-above-max"),
        pytest.param("lock_timeout_seconds", True, id="timeout-true"),
        pytest.param("lock_timeout_seconds", False, id="timeout-false"),
        pytest.param("lock_timeout_seconds", None, id="timeout-none"),
        pytest.param("lock_timeout_seconds", "1.0", id="timeout-string"),
        pytest.param("lock_timeout_seconds", object(), id="timeout-object"),
        pytest.param("lock_poll_interval_seconds", float("nan"), id="poll-nan"),
        pytest.param("lock_poll_interval_seconds", float("inf"), id="poll-pos-inf"),
        pytest.param("lock_poll_interval_seconds", float("-inf"), id="poll-neg-inf"),
        pytest.param("lock_poll_interval_seconds", 0, id="poll-zero-int"),
        pytest.param("lock_poll_interval_seconds", 0.0, id="poll-zero-float"),
        pytest.param("lock_poll_interval_seconds", -0.01, id="poll-negative"),
        pytest.param("lock_poll_interval_seconds", 1.1, id="poll-above-timeout"),
        pytest.param("lock_poll_interval_seconds", True, id="poll-true"),
        pytest.param("lock_poll_interval_seconds", False, id="poll-false"),
        pytest.param("lock_poll_interval_seconds", None, id="poll-none"),
        pytest.param("lock_poll_interval_seconds", "0.01", id="poll-string"),
        pytest.param("lock_poll_interval_seconds", object(), id="poll-object"),
    ],
)
def test_lock_timing_rejects_invalid_values_before_db_or_sleep(field, value):
    session = _NoDbSession()
    kwargs = {field: value, "lock_sleep": _unexpected_sleep}
    with pytest.raises(ValueError, match="synthetic organization lock .* is invalid"):
        SyntheticOrganizationRepository(session, **kwargs)
    assert session.touched is False


def test_lock_timing_accepts_defaults_and_valid_boundaries():
    default_repo = SyntheticOrganizationRepository(_NoDbSession())
    assert default_repo._lock_timeout_seconds == SYNTHETIC_ORGANIZATION_LOCK_TIMEOUT_SECONDS
    assert default_repo._lock_poll_interval_seconds == SYNTHETIC_ORGANIZATION_LOCK_POLL_INTERVAL_SECONDS

    tiny_repo = SyntheticOrganizationRepository(_NoDbSession(), lock_timeout_seconds=0.001, lock_poll_interval_seconds=0.0005)
    assert math.isclose(tiny_repo._lock_timeout_seconds, 0.001)
    assert math.isclose(tiny_repo._lock_poll_interval_seconds, 0.0005)

    max_repo = SyntheticOrganizationRepository(_NoDbSession(), lock_timeout_seconds=30, lock_poll_interval_seconds=30)
    assert max_repo._lock_timeout_seconds == 30.0
    assert max_repo._lock_poll_interval_seconds == 30.0

    ordinary_repo = SyntheticOrganizationRepository(_NoDbSession(), lock_timeout_seconds=2, lock_poll_interval_seconds=0.25)
    assert ordinary_repo._lock_timeout_seconds == 2.0
    assert ordinary_repo._lock_poll_interval_seconds == 0.25
