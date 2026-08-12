from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_reduced_runtime_effectively_inherits_member_domain_capability(db_session) -> None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT
                    session_user::text AS session_name,
                    current_user::text AS current_name,
                    pg_catalog.pg_has_role(current_user, 'app_runtime', 'MEMBER') AS app_member,
                    pg_catalog.pg_has_role(current_user, 'app_runtime', 'USAGE') AS app_usage,
                    pg_catalog.pg_has_role(current_user, 'app_runtime', 'SET') AS app_set,
                    pg_catalog.has_table_privilege('app_runtime', 'public.members', 'SELECT') AS capability_members_select,
                    pg_catalog.has_table_privilege(current_user, 'public.members', 'SELECT') AS effective_members_select,
                    pg_catalog.has_table_privilege(current_user, 'public.members', 'INSERT') AS effective_members_insert,
                    pg_catalog.has_table_privilege(current_user, 'public.members', 'DELETE') AS effective_members_delete,
                    pg_catalog.has_table_privilege(current_user, 'public.members', 'TRUNCATE') AS effective_members_truncate,
                    pg_catalog.has_column_privilege(current_user, 'public.members', 'name', 'UPDATE') AS effective_member_name_update,
                    pg_catalog.has_column_privilege(current_user, 'public.members', 'org_id', 'UPDATE') AS effective_member_org_update,
                    pg_catalog.has_table_privilege(current_user, 'public.membership_plans', 'SELECT') AS effective_plan_select,
                    pg_catalog.has_table_privilege(current_user, 'public.member_subscriptions_v2', 'INSERT') AS effective_subscription_insert,
                    pg_catalog.has_table_privilege(current_user, 'public.subscription_members', 'INSERT') AS effective_slot_insert
                """
            )
        )
    ).mappings().one()

    assert row["session_name"] == row["current_name"]
    assert row["current_name"] not in {
        "migration_owner",
        "auth_test_runtime",
        "lifecycle_maintenance_test_runtime",
    }

    assert row["capability_members_select"], "app_runtime lost its revision-92 members SELECT ACL"
    assert row["app_member"], "runtime login is not a member of app_runtime"
    assert row["app_usage"], "runtime login does not inherit the app_runtime capability"
    assert not row["app_set"], "runtime login must not be able to SET ROLE app_runtime"

    assert row["effective_members_select"]
    assert row["effective_members_insert"]
    assert row["effective_member_name_update"]
    assert row["effective_plan_select"]
    assert row["effective_subscription_insert"]
    assert row["effective_slot_insert"]

    assert not row["effective_members_delete"]
    assert not row["effective_members_truncate"]
    assert not row["effective_member_org_update"]
