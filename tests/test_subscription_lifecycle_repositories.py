from datetime import date
from uuid import UUID

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.domain.subscription_lifecycle import (
    SubscriptionOperationalStatus,
    SubscriptionSeriesStatus,
)
from app.repositories.subscription_lifecycle_repo import SubscriptionLifecycleRepository
from test_subscription_lifecycle_migrations import (
    BRANCH_1,
    MEMBER_100,
    ORG_1,
    ORG_2,
    SUB_A,
    SUB_B,
    SUB_FAMILY,
    prepare_migrated_lifecycle,
)


BUSINESS_DATE = date(2026, 6, 20)


async def _term_id_for_legacy(session, legacy_subscription_id: str):
    return (
        await session.execute(
            text("SELECT id FROM subscription_terms WHERE legacy_member_subscription_v2_id = :legacy_id"),
            {"legacy_id": legacy_subscription_id},
        )
    ).scalar_one()


async def _series_id_for_legacy(session, legacy_subscription_id: str):
    return (
        await session.execute(
            text("SELECT series_id FROM subscription_terms WHERE legacy_member_subscription_v2_id = :legacy_id"),
            {"legacy_id": legacy_subscription_id},
        )
    ).scalar_one()


async def _seed_phase3_read_overlays():
    async with AsyncSessionLocal() as session:
        term_a_id = await _term_id_for_legacy(session, SUB_A)
        series_a_id = await _series_id_for_legacy(session, SUB_A)

        await session.execute(
            text(
                """
                INSERT INTO subscription_freezes (
                    id, org_id, series_id, term_id, status, requested_starts_on,
                    planned_ends_on, extension_days, reason
                )
                VALUES (
                    '91000000-0000-0000-0000-000000000001', :org_id, :series_id, :term_id,
                    'active'::subscription_freeze_status, DATE '2026-06-18',
                    DATE '2026-06-25', 7, 'Medical hold'
                )
                """
            ),
            {"org_id": ORG_1, "series_id": series_a_id, "term_id": term_a_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO subscription_events (
                    id, org_id, branch_id, series_id, term_id, event_type,
                    event_at, event_source, metadata
                )
                VALUES (
                    '92000000-0000-0000-0000-000000000001', :org_id, :branch_id, :series_id,
                    :term_id, 'freeze_started'::subscription_event_type,
                    TIMESTAMPTZ '2026-06-18 10:00:00+00', 'test', '{"reason":"Medical hold"}'::jsonb
                )
                """
            ),
            {
                "org_id": ORG_1,
                "branch_id": BRANCH_1,
                "series_id": series_a_id,
                "term_id": term_a_id,
            },
        )
        await session.execute(
            text(
                """
                UPDATE subscription_series
                SET lifecycle_status = 'archived'::subscription_series_status,
                    archived_at = TIMESTAMPTZ '2026-06-19 10:00:00+00'
                WHERE id = (
                    SELECT series_id
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :sub_family
                )
                """
            ),
            {"sub_family": SUB_FAMILY},
        )
        await session.commit()

        return term_a_id, series_a_id


async def test_series_summaries_are_tenant_scoped_and_derive_current_status():
    await prepare_migrated_lifecycle()
    term_a_id, series_a_id = await _seed_phase3_read_overlays()

    async with AsyncSessionLocal() as session:
        repo = SubscriptionLifecycleRepository(session)
        summaries, total = await repo.list_series_summaries(
            UUID(ORG_1),
            business_date=BUSINESS_DATE,
            include_archived=True,
        )

        assert total == 3
        by_term_code = {
            summary.current_term.term_code if summary.current_term else summary.scheduled_next_term.term_code: summary
            for summary in summaries
        }
        assert by_term_code["SUB-ADJ-001"].operational_status == SubscriptionOperationalStatus.frozen
        assert by_term_code["SUB-ADJ-001"].current_term.id == term_a_id
        assert by_term_code["SUB-ADJ-001"].occupied_slots == 1
        assert by_term_code["SUB-ADJ-001"].current_freeze.reason == "Medical hold"
        assert "resume" in by_term_code["SUB-ADJ-001"].available_actions
        assert by_term_code["SUB-ADJ-002"].scheduled_next_term is not None
        assert by_term_code["SUB-ADJ-002"].scheduled_next_term.derived_status == SubscriptionOperationalStatus.scheduled
        assert by_term_code["SUB-FAM-001"].lifecycle_status == SubscriptionSeriesStatus.archived

        org2_summaries, org2_total = await repo.list_series_summaries(
            UUID(ORG_2),
            business_date=BUSINESS_DATE,
        )
        assert org2_total == 1
        assert {summary.org_id for summary in org2_summaries} == {UUID(ORG_2)}

        detail = await repo.get_series_detail(UUID(ORG_1), series_a_id, business_date=BUSINESS_DATE)
        assert detail.series_code == "SER-SUB-ADJ-001"
        assert detail.current_term.term_code == "SUB-ADJ-001"


async def test_repository_reads_slots_timeline_upcoming_history_and_projection():
    await prepare_migrated_lifecycle()
    term_a_id, series_a_id = await _seed_phase3_read_overlays()

    async with AsyncSessionLocal() as session:
        repo = SubscriptionLifecycleRepository(session)

        slots = await repo.list_slots(UUID(ORG_1), term_a_id, business_date=BUSINESS_DATE)
        assert len(slots) == 1
        assert slots[0].current_member.id == UUID(MEMBER_100)
        assert not slots[0].is_vacant

        timeline, timeline_total = await repo.list_timeline(UUID(ORG_1), series_a_id)
        assert timeline_total == 1
        assert timeline[0].event_type.value == "freeze_started"
        assert timeline[0].metadata["reason"] == "Medical hold"

        upcoming, upcoming_total = await repo.list_upcoming_terms(
            UUID(ORG_1),
            business_date=BUSINESS_DATE,
        )
        assert upcoming_total == 1
        assert upcoming[0].term_code == "SUB-ADJ-002"
        assert upcoming[0].derived_status == SubscriptionOperationalStatus.scheduled

        history, history_total = await repo.list_history_terms(
            UUID(ORG_1),
            business_date=date(2027, 1, 1),
            member_id=UUID(MEMBER_100),
        )
        assert history_total >= 2
        assert {"SUB-ADJ-001", "SUB-ADJ-002"}.issubset({term.term_code for term in history})

        projection = await repo.get_v2_projection(UUID(ORG_1), series_a_id, business_date=BUSINESS_DATE)
        assert projection is not None
        assert projection.subscription_code == "SUB-ADJ-001"
        assert projection.status == SubscriptionOperationalStatus.frozen
        assert projection.duration_value_snapshot == 3
        assert projection.duration_unit_snapshot == "months"
