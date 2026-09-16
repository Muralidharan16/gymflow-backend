from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import update_session_context
from app.domain.subscription_lifecycle import (
    SubscriptionOperationalStatus,
    SubscriptionSeriesStatus,
)
from app.repositories.subscription_lifecycle_repo import SubscriptionLifecycleRepository
from conftest import TEST_DATABASE_URL
from test_subscription_lifecycle_migrations import (
    CONSTRAINT_REVISION,
    LifecycleSeed,
    MIGRATION_TEST_DATABASE_URL,
    MigrationTestSessionLocal,
    prepare_migrated_lifecycle,
    run_alembic,
)


# Repository semantics must be exercised through the reduced application
# runtime identity, but against the dedicated database that owns the migration
# rehearsal data.  This keeps schema mutation on migration_owner while proving
# the application-facing read boundary on the exact migrated state.
_migration_database = make_url(MIGRATION_TEST_DATABASE_URL).database
_runtime_url = make_url(TEST_DATABASE_URL).set(database=_migration_database)
lifecycle_runtime_engine = create_async_engine(
    _runtime_url,
    poolclass=NullPool,
    pool_pre_ping=True,
    echo=False,
)
LifecycleRuntimeSessionLocal = async_sessionmaker(
    lifecycle_runtime_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _term_codes(seed: LifecycleSeed) -> tuple[str, str, str]:
    return (
        f"SUB-ADJ-A-{seed.suffix}",
        f"SUB-ADJ-B-{seed.suffix}",
        f"SUB-FAM-{seed.suffix}",
    )


async def _install_runtime_context(
    session: AsyncSession,
    seed: LifecycleSeed,
    *,
    org_id: str | None = None,
    owner_id: str | None = None,
) -> None:
    await update_session_context(
        session,
        principal_id=owner_id or seed.owner_1,
        principal_type="owner",
        org_id=org_id or seed.org_1,
        role="owner",
        ip_address="127.0.0.1",
        user_agent="pytest-subscription-lifecycle-repository",
        request_id=f"lifecycle-repo-{seed.suffix}",
    )


async def _term_id_for_legacy(session: AsyncSession, legacy_subscription_id: str):
    return (
        await session.execute(
            text("SELECT id FROM subscription_terms WHERE legacy_member_subscription_v2_id = :legacy_id"),
            {"legacy_id": legacy_subscription_id},
        )
    ).scalar_one()


async def _series_id_for_legacy(session: AsyncSession, legacy_subscription_id: str):
    return (
        await session.execute(
            text("SELECT series_id FROM subscription_terms WHERE legacy_member_subscription_v2_id = :legacy_id"),
            {"legacy_id": legacy_subscription_id},
        )
    ).scalar_one()


async def _seed_phase3_read_overlays(seed: LifecycleSeed):
    """Add read-model overlays before later runtime RLS hardening.

    The lifecycle integrity triggers validate member/plan ownership by reading
    the legacy source tables.  Current HEAD deliberately FORCE-enables RLS on
    those source tables and scopes them to app_runtime, so migration_owner must
    not be granted a runtime visibility path merely to mutate test fixtures.
    Repository fixtures are therefore written at the lifecycle constraint
    revision and the dedicated migration database is upgraded to HEAD before
    any reduced-runtime repository read is exercised.
    """
    async with MigrationTestSessionLocal() as session:
        term_a = (
            await session.execute(
                text(
                    """
                    SELECT id, series_id, starts_on, effective_ends_on
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :legacy_id
                    """
                ),
                {"legacy_id": seed.sub_a},
            )
        ).mappings().one()
        term_a_id = term_a["id"]
        series_a_id = term_a["series_id"]

        # Derive every overlay date from the migrated term itself. This keeps
        # the repository fixture valid across calendar rollovers while still
        # exercising a current term, an active freeze and an upcoming term.
        business_date = term_a["starts_on"] + timedelta(days=15)
        assert business_date <= term_a["effective_ends_on"]
        freeze_start = business_date - timedelta(days=2)
        freeze_end = business_date + timedelta(days=5)
        archive_date = business_date - timedelta(days=1)

        await session.execute(
            text(
                """
                INSERT INTO subscription_freezes (
                    id, org_id, series_id, term_id, status, requested_starts_on,
                    planned_ends_on, extension_days, reason
                )
                VALUES (
                    gen_random_uuid(), :org_id, :series_id, :term_id,
                    'active'::subscription_freeze_status, :freeze_start,
                    :freeze_end, 7, 'Medical hold'
                )
                """
            ),
            {
                "org_id": seed.org_1,
                "series_id": series_a_id,
                "term_id": term_a_id,
                "freeze_start": freeze_start,
                "freeze_end": freeze_end,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO subscription_events (
                    id, org_id, branch_id, series_id, term_id, event_type,
                    event_at, event_source, metadata
                )
                VALUES (
                    gen_random_uuid(), :org_id, :branch_id, :series_id,
                    :term_id, 'freeze_started'::subscription_event_type,
                    CAST(:freeze_start AS date) + TIME '10:00:00', 'test',
                    '{"reason":"Medical hold"}'::jsonb
                )
                """
            ),
            {
                "org_id": seed.org_1,
                "branch_id": seed.branch_1,
                "series_id": series_a_id,
                "term_id": term_a_id,
                "freeze_start": freeze_start,
            },
        )
        await session.execute(
            text(
                """
                UPDATE subscription_series
                SET lifecycle_status = 'archived'::subscription_series_status,
                    archived_at = CAST(:archive_date AS date) + TIME '10:00:00'
                WHERE id = (
                    SELECT series_id
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :sub_family
                )
                """
            ),
            {"sub_family": seed.sub_family, "archive_date": archive_date},
        )
        await session.commit()
        return term_a_id, series_a_id, business_date


async def _prepare_repository_state():
    """Stage fixtures at the constraint revision, then verify reads at HEAD."""
    seed = await prepare_migrated_lifecycle(CONSTRAINT_REVISION)
    try:
        term_a_id, series_a_id, business_date = await _seed_phase3_read_overlays(seed)
    finally:
        # Restore current HEAD even when fixture staging fails.  Later runtime
        # hardening remains part of every repository-read test and is never
        # bypassed by the migration identity.
        run_alembic("upgrade", "head")
    return seed, term_a_id, series_a_id, business_date


async def test_series_summaries_are_tenant_scoped_and_derive_current_status():
    seed, term_a_id, series_a_id, business_date = await _prepare_repository_state()
    code_a, code_b, code_family = _term_codes(seed)

    async with LifecycleRuntimeSessionLocal() as session:
        await _install_runtime_context(session, seed)
        repo = SubscriptionLifecycleRepository(session)
        summaries, total = await repo.list_series_summaries(
            UUID(seed.org_1),
            business_date=business_date,
            include_archived=True,
        )

        assert total == 3
        by_term_code = {
            summary.current_term.term_code if summary.current_term else summary.scheduled_next_term.term_code: summary
            for summary in summaries
        }
        assert by_term_code[code_a].operational_status == SubscriptionOperationalStatus.frozen
        assert by_term_code[code_a].current_term.id == term_a_id
        assert by_term_code[code_a].occupied_slots == 1
        assert by_term_code[code_a].current_freeze.reason == "Medical hold"
        assert "resume" in by_term_code[code_a].available_actions
        assert by_term_code[code_b].scheduled_next_term is not None
        assert by_term_code[code_b].scheduled_next_term.derived_status == SubscriptionOperationalStatus.scheduled
        assert by_term_code[code_family].lifecycle_status == SubscriptionSeriesStatus.archived

        detail = await repo.get_series_detail(UUID(seed.org_1), series_a_id, business_date=business_date)
        assert detail.series_code == f"SER-{code_a}"
        assert detail.current_term.term_code == code_a

    # A separate transaction installs the second tenant context.  This proves
    # the reduced runtime cannot obtain org-2 data while operating as org-1.
    async with LifecycleRuntimeSessionLocal() as session:
        await _install_runtime_context(
            session,
            seed,
            org_id=seed.org_2,
            owner_id=seed.owner_2,
        )
        repo = SubscriptionLifecycleRepository(session)
        org2_summaries, org2_total = await repo.list_series_summaries(
            UUID(seed.org_2),
            business_date=business_date,
        )
        assert org2_total == 1
        assert {summary.org_id for summary in org2_summaries} == {UUID(seed.org_2)}


async def test_repository_reads_slots_timeline_upcoming_history_and_projection():
    seed, term_a_id, series_a_id, business_date = await _prepare_repository_state()
    code_a, code_b, _ = _term_codes(seed)

    async with LifecycleRuntimeSessionLocal() as session:
        await _install_runtime_context(session, seed)
        repo = SubscriptionLifecycleRepository(session)

        slots = await repo.list_slots(UUID(seed.org_1), term_a_id, business_date=business_date)
        assert len(slots) == 1
        assert slots[0].current_member.id == UUID(seed.member_100)
        assert not slots[0].is_vacant

        timeline, timeline_total = await repo.list_timeline(UUID(seed.org_1), series_a_id)
        assert timeline_total == 1
        assert timeline[0].event_type.value == "freeze_started"
        assert timeline[0].metadata["reason"] == "Medical hold"

        upcoming, upcoming_total = await repo.list_upcoming_terms(
            UUID(seed.org_1),
            business_date=business_date,
        )
        assert upcoming_total == 1
        assert upcoming[0].term_code == code_b
        assert upcoming[0].derived_status == SubscriptionOperationalStatus.scheduled

        history, history_total = await repo.list_history_terms(
            UUID(seed.org_1),
            business_date=business_date + timedelta(days=365),
            member_id=UUID(seed.member_100),
        )
        assert history_total >= 2
        assert {code_a, code_b}.issubset({term.term_code for term in history})

        projection = await repo.get_v2_projection(UUID(seed.org_1), series_a_id, business_date=business_date)
        assert projection is not None
        assert projection.subscription_code == code_a
        assert projection.status == SubscriptionOperationalStatus.frozen
        assert projection.duration_value_snapshot == 3
        assert projection.duration_unit_snapshot == "months"
