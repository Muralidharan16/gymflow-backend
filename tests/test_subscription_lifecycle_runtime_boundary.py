from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text

from app.repositories.subscription_lifecycle_repo import SubscriptionLifecycleRepository
from test_subscription_lifecycle_migrations import (
    LifecycleRuntimeSessionLocal,
    prepare_migrated_lifecycle,
    set_runtime_tenant_context,
)


_TABLES = (
    "subscription_series",
    "subscription_terms",
    "subscription_term_slots",
    "subscription_slot_assignments",
    "subscription_freezes",
    "subscription_events",
)
_FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


async def test_lifecycle_runtime_boundary_is_select_only_and_tenant_enforced():
    seed = await prepare_migrated_lifecycle()

    async with LifecycleRuntimeSessionLocal() as session:
        await set_runtime_tenant_context(session, seed.org_1, seed.owner_1)

        for table_name in _TABLES:
            can_select = (
                await session.execute(
                    text("SELECT pg_catalog.has_table_privilege(current_user, :relation, 'SELECT')"),
                    {"relation": f"public.{table_name}"},
                )
            ).scalar_one()
            assert can_select is True

            for privilege in _FORBIDDEN:
                has_forbidden = (
                    await session.execute(
                        text(
                            "SELECT pg_catalog.has_table_privilege(current_user, :relation, :privilege)"
                        ),
                        {"relation": f"public.{table_name}", "privilege": privilege},
                    )
                ).scalar_one()
                assert has_forbidden is False, f"unexpected {privilege} on {table_name}"

            own_count = (
                await session.execute(
                    text(f"SELECT count(*) FROM public.{table_name} WHERE org_id = :org_id"),
                    {"org_id": seed.org_1},
                )
            ).scalar_one()
            cross_tenant_count = (
                await session.execute(
                    text(f"SELECT count(*) FROM public.{table_name} WHERE org_id = :org_id"),
                    {"org_id": seed.org_2},
                )
            ).scalar_one()
            assert int(cross_tenant_count) == 0
            if table_name in {
                "subscription_series",
                "subscription_terms",
                "subscription_term_slots",
                "subscription_slot_assignments",
            }:
                assert int(own_count) > 0

        # Even a repository call that explicitly asks for another tenant cannot
        # escape the transaction-local RLS tenant bound.
        repo = SubscriptionLifecycleRepository(session)
        blocked, blocked_total = await repo.list_series_summaries(
            UUID(seed.org_2),
            business_date=date(2026, 6, 20),
            include_archived=True,
        )
        assert blocked_total == 0
        assert blocked == []
