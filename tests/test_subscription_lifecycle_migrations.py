from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from conftest import cleanup_test_database_tables


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "b1c2d3e4f5a6"

ORG_1 = "10000000-0000-0000-0000-000000000001"
ORG_2 = "10000000-0000-0000-0000-000000000002"
OWNER_1 = "20000000-0000-0000-0000-000000000001"
OWNER_2 = "20000000-0000-0000-0000-000000000002"
BRANCH_1 = "30000000-0000-0000-0000-000000000001"
BRANCH_2 = "30000000-0000-0000-0000-000000000002"
BRANCH_3 = "30000000-0000-0000-0000-000000000003"
MEMBER_100 = "40000000-0000-0000-0000-000000000100"
MEMBER_101 = "40000000-0000-0000-0000-000000000101"
MEMBER_200 = "40000000-0000-0000-0000-000000000200"
PLAN_INDIVIDUAL = "50000000-0000-0000-0000-000000000001"
PLAN_FAMILY = "50000000-0000-0000-0000-000000000002"
PLAN_OTHER = "50000000-0000-0000-0000-000000000003"
SUB_A = "60000000-0000-0000-0000-000000000001"
SUB_B = "60000000-0000-0000-0000-000000000002"
SUB_FAMILY = "60000000-0000-0000-0000-000000000003"
SUB_EXPIRED = "60000000-0000-0000-0000-000000000004"

ALL_TABLES = [
    "subscription_slot_assignments",
    "subscription_term_slots",
    "subscription_freezes",
    "subscription_events",
    "subscription_operation_idempotency",
    "subscription_terms",
    "subscription_series",
    "subscription_members",
    "member_subscriptions_v2",
    "members",
    "membership_plans",
    "organization_counters",
    "org_branch_state",
    "org_branches",
    "owners",
    "organizations",
]


def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = env["TEST_DATABASE_URL"]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


async def clean_seed_tables() -> None:
    await cleanup_test_database_tables(ALL_TABLES)


async def seed_v2_source_data() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        seed_sql = """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES
                    (:org1, 'Lifecycle Org 1', 'lifecycle-org-1', 'basic'::orgtier, true, 5, 'INR'),
                    (:org2, 'Lifecycle Org 2', 'lifecycle-org-2', 'basic'::orgtier, true, 5, 'INR');

                INSERT INTO owners (id, org_id, owner_name, email, hashed_password, email_verified)
                VALUES
                    (:owner1, :org1, 'Owner One', 'lifecycle-owner-1@test.local', 'hash', true),
                    (:owner2, :org2, 'Owner Two', 'lifecycle-owner-2@test.local', 'hash', true);

                INSERT INTO org_branches (id, org_id, branch_name, branch_code, internal_slug, timezone, currency_code, country_code, created_by)
                VALUES
                    (:branch1, :org1, 'Lifecycle Main', 'MAIN', 'lifecycle-main', 'Asia/Kolkata', 'INR', 'IN', :owner1),
                    (:branch2, :org1, 'Lifecycle Annex', 'ANNEX', 'lifecycle-annex', 'Asia/Kolkata', 'INR', 'IN', :owner1),
                    (:branch3, :org2, 'Lifecycle Other', 'OTHER', 'lifecycle-other', 'Asia/Kolkata', 'INR', 'IN', :owner2);

                INSERT INTO members (id, org_id, home_branch_id, member_uid, member_number, name, phone, status, is_active, is_migrated)
                VALUES
                    (:member100, :org1, :branch1, 'LIFE-100', 100, 'Member One Hundred', '9000000100', 'active'::memberstatus, true, false),
                    (:member101, :org1, :branch1, 'LIFE-101', 101, 'Member One Hundred One', '9000000101', 'active'::memberstatus, true, false),
                    (:member200, :org2, :branch3, 'LIFE-200', 200, 'Member Two Hundred', '9000000200', 'active'::memberstatus, true, false);

                INSERT INTO membership_plans (
                    id, org_id, branch_id, plan_code, name, price, currency, duration_value, duration_unit, max_members, status
                )
                VALUES
                    (:planIndividual, :org1, :branch1, 'IND-QUARTER', 'Individual Quarterly', 3500.00, 'INR', 3, 'months'::duration_unit, 1, 'active'::plan_status),
                    (:planFamily, :org1, :branch1, 'FAM-QUARTER', 'Family Quarterly', 4000.00, 'INR', 3, 'months'::duration_unit, 3, 'active'::plan_status),
                    (:planOther, :org2, :branch3, 'OTHER-MONTH', 'Other Monthly', 1200.00, 'INR', 1, 'months'::duration_unit, 1, 'active'::plan_status);

                INSERT INTO member_subscriptions_v2 (
                    id, org_id, branch_id, membership_plan_id, primary_member_id, subscription_code,
                    start_date, end_date, status, price_snapshot, currency_code, duration_value_snapshot,
                    duration_unit_snapshot, max_members_snapshot, created_by, updated_by, created_at, updated_at
                )
                VALUES
                    (:subA, :org1, :branch1, :planIndividual, :member100, 'SUB-ADJ-001',
                     DATE '2026-06-15', DATE '2026-09-15', 'active'::modern_subscription_status,
                     3500.00, 'INR', 3, 'months'::duration_unit, 1, :owner1, :owner1, now(), now()),
                    (:subB, :org1, :branch1, :planIndividual, :member100, 'SUB-ADJ-002',
                     DATE '2026-09-16', DATE '2026-12-16', 'active'::modern_subscription_status,
                     3500.00, 'INR', 3, 'months'::duration_unit, 1, :owner1, :owner1, now(), now()),
                    (:subFamily, :org1, :branch1, :planFamily, :member101, 'SUB-FAM-001',
                     DATE '2026-06-15', DATE '2026-09-15', 'active'::modern_subscription_status,
                     4000.00, 'INR', 3, 'months'::duration_unit, 3, :owner1, :owner1, now(), now()),
                    (:subExpired, :org2, :branch3, :planOther, :member200, 'SUB-EXP-001',
                     DATE '2025-01-01', DATE '2025-02-01', 'expired'::modern_subscription_status,
                     1200.00, 'INR', 1, 'months'::duration_unit, 1, :owner2, :owner2, now(), now());

                INSERT INTO subscription_members (
                    id, org_id, subscription_id, member_id, slot_number, role, is_active, joined_at, created_at, updated_at
                )
                VALUES
                    ('70000000-0000-0000-0000-000000000001', :org1, :subA, :member100, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
                    ('70000000-0000-0000-0000-000000000002', :org1, :subB, :member100, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
                    ('70000000-0000-0000-0000-000000000003', :org1, :subFamily, :member101, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
                    ('70000000-0000-0000-0000-000000000004', :org1, :subFamily, :member100, 2, 'additional'::subscription_member_role, true, now(), now(), now()),
                    ('70000000-0000-0000-0000-000000000005', :org2, :subExpired, :member200, 1, 'primary'::subscription_member_role, true, now(), now(), now());
        """
        params = {
            "org1": ORG_1,
            "org2": ORG_2,
            "owner1": OWNER_1,
            "owner2": OWNER_2,
            "branch1": BRANCH_1,
            "branch2": BRANCH_2,
            "branch3": BRANCH_3,
            "member100": MEMBER_100,
            "member101": MEMBER_101,
            "member200": MEMBER_200,
            "planIndividual": PLAN_INDIVIDUAL,
            "planFamily": PLAN_FAMILY,
            "planOther": PLAN_OTHER,
            "subA": SUB_A,
            "subB": SUB_B,
            "subFamily": SUB_FAMILY,
            "subExpired": SUB_EXPIRED,
        }
        for statement in [statement.strip() for statement in seed_sql.split(";") if statement.strip()]:
            await session.execute(text(statement), params)
        await session.commit()


async def scalar_int(sql: str, params: dict[str, object] | None = None) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return int(result.scalar_one())


async def assert_table_absent(table_name: str) -> None:
    exists = await scalar_int(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = :table_name
        """,
        {"table_name": table_name},
    )
    assert exists == 0


async def expect_db_error(sql: str, params: dict[str, object]) -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception):
            await session.execute(text(sql), params)
            await session.commit()
        await session.rollback()


async def prepare_migrated_lifecycle() -> None:
    run_alembic("upgrade", "head")
    await clean_seed_tables()
    run_alembic("downgrade", BASELINE_REVISION)
    await clean_seed_tables()
    await seed_v2_source_data()
    run_alembic("upgrade", "head")


async def test_lifecycle_migration_round_trip_preserves_v2_and_backfills_conservatively():
    await prepare_migrated_lifecycle()

    assert await scalar_int("SELECT count(*) FROM member_subscriptions_v2") == 4
    assert await scalar_int("SELECT count(*) FROM subscription_series") == 4
    assert await scalar_int("SELECT count(*) FROM subscription_terms") == 4
    assert await scalar_int("SELECT count(*) FROM subscription_term_slots") == 6
    assert await scalar_int("SELECT count(*) FROM subscription_slot_assignments") == 5
    assert await scalar_int("SELECT count(*) FROM subscription_terms WHERE renewed_from_term_id IS NOT NULL") == 0

    adjacent_series_count = await scalar_int(
        """
        SELECT count(DISTINCT st.series_id)
        FROM subscription_terms st
        WHERE st.legacy_member_subscription_v2_id IN (:sub_a, :sub_b)
        """,
        {"sub_a": SUB_A, "sub_b": SUB_B},
    )
    assert adjacent_series_count == 2

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        term_code,
                        legacy_subscription_code,
                        plan_code_snapshot,
                        plan_name_snapshot,
                        capacity_snapshot,
                        list_price_amount,
                        final_amount
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :sub_family
                    """
                ),
                {"sub_family": SUB_FAMILY},
            )
        ).one()
        assert row.term_code == "SUB-FAM-001"
        assert row.legacy_subscription_code == "SUB-FAM-001"
        assert row.plan_code_snapshot == "FAM-QUARTER"
        assert row.plan_name_snapshot == "Family Quarterly"
        assert row.capacity_snapshot == 3
        assert str(row.list_price_amount) == "4000.00"
        assert str(row.final_amount) == "4000.00"

    family_slot_count = await scalar_int(
        """
        SELECT count(*)
        FROM subscription_term_slots slots
        JOIN subscription_terms terms ON terms.id = slots.term_id
        WHERE terms.legacy_member_subscription_v2_id = :sub_family
        """,
        {"sub_family": SUB_FAMILY},
    )
    family_assignment_count = await scalar_int(
        """
        SELECT count(*)
        FROM subscription_slot_assignments assignments
        JOIN subscription_terms terms ON terms.id = assignments.term_id
        WHERE terms.legacy_member_subscription_v2_id = :sub_family
        """,
        {"sub_family": SUB_FAMILY},
    )
    assert family_slot_count == 3
    assert family_assignment_count == 2

    run_alembic("downgrade", BASELINE_REVISION)
    await assert_table_absent("subscription_series")
    assert await scalar_int("SELECT count(*) FROM member_subscriptions_v2") == 4
    assert await scalar_int("SELECT count(*) FROM subscription_members") == 5

    run_alembic("upgrade", "head")
    assert await scalar_int("SELECT count(*) FROM subscription_series") == 4
    assert await scalar_int("SELECT count(*) FROM subscription_terms") == 4


async def test_lifecycle_constraints_enforce_overlap_tenant_lineage_and_assignment_integrity():
    await prepare_migrated_lifecycle()

    async with AsyncSessionLocal() as session:
        term = (
            await session.execute(
                text(
                    """
                    SELECT id, org_id, branch_id, series_id, plan_id, effective_ends_on
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :sub_a
                    """
                ),
                {"sub_a": SUB_A},
            )
        ).mappings().one()
        other_series = (
            await session.execute(
                text(
                    """
                    SELECT series_id
                    FROM subscription_terms
                    WHERE legacy_member_subscription_v2_id = :sub_family
                    """
                ),
                {"sub_family": SUB_FAMILY},
            )
        ).scalar_one()
        slot_id = (
            await session.execute(
                text("SELECT id FROM subscription_term_slots WHERE term_id = :term_id AND slot_index = 1"),
                {"term_id": term["id"]},
            )
        ).scalar_one()

    base_params = {
        "org_id": term["org_id"],
        "branch_id": term["branch_id"],
        "series_id": term["series_id"],
        "plan_id": term["plan_id"],
        "parent_term_id": term["id"],
        "other_series_id": other_series,
        "slot_id": slot_id,
        "member100": MEMBER_100,
        "owner1": OWNER_1,
    }

    await expect_db_error(
        """
        INSERT INTO subscription_terms (
            id, org_id, branch_id, series_id, sequence_number, term_code, source_type, plan_id,
            plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
            capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
            starts_on, base_ends_on, effective_ends_on, status
        )
        VALUES (
            '80000000-0000-0000-0000-000000000001', :org_id, :branch_id, :series_id, 99,
            'BAD-DATES', 'admin_adjustment'::subscription_term_source, :plan_id,
            'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR',
            3500.00, 0, 0, 3500.00, DATE '2026-10-01', DATE '2026-09-01',
            DATE '2026-09-01', 'scheduled'::subscription_term_status
        )
        """,
        base_params,
    )

    await expect_db_error(
        """
        INSERT INTO subscription_terms (
            id, org_id, branch_id, series_id, sequence_number, term_code, source_type, plan_id,
            plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
            capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
            starts_on, base_ends_on, effective_ends_on, status
        )
        VALUES (
            '80000000-0000-0000-0000-000000000002', :org_id, :branch_id, :series_id, 99,
            'BAD-AMOUNT', 'admin_adjustment'::subscription_term_source, :plan_id,
            'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR',
            -1.00, 0, 0, 3500.00, DATE '2026-10-01', DATE '2026-12-01',
            DATE '2026-12-01', 'scheduled'::subscription_term_status
        )
        """,
        base_params,
    )

    await expect_db_error(
        """
        INSERT INTO subscription_terms (
            id, org_id, branch_id, series_id, sequence_number, term_code, source_type, plan_id,
            plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
            capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
            starts_on, base_ends_on, effective_ends_on, status
        )
        VALUES (
            '80000000-0000-0000-0000-000000000003', :org_id, :branch_id, :series_id, 99,
            'OVERLAP', 'admin_adjustment'::subscription_term_source, :plan_id,
            'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR',
            3500.00, 0, 0, 3500.00, DATE '2026-07-01', DATE '2026-08-01',
            DATE '2026-08-01', 'scheduled'::subscription_term_status
        )
        """,
        base_params,
    )

    await expect_db_error(
        """
        INSERT INTO subscription_terms (
            id, org_id, branch_id, series_id, sequence_number, term_code, renewed_from_term_id,
            source_type, plan_id, plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot,
            duration_value_snapshot, capacity_snapshot, currency_code, list_price_amount,
            discount_amount, tax_amount, final_amount, starts_on, base_ends_on, effective_ends_on, status
        )
        VALUES (
            '80000000-0000-0000-0000-000000000004', :org_id, :branch_id, :other_series_id, 99,
            'CROSS-LINEAGE', :parent_term_id, 'renewal'::subscription_term_source, :plan_id,
            'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR',
            3500.00, 0, 0, 3500.00, DATE '2026-10-01', DATE '2026-12-01',
            DATE '2026-12-01', 'scheduled'::subscription_term_status
        )
        """,
        base_params,
    )

    await expect_db_error(
        """
        INSERT INTO subscription_slot_assignments (
            id, org_id, term_id, term_slot_id, member_id, effective_from, effective_until,
            assignment_state, assigned_by
        )
        VALUES (
            '81000000-0000-0000-0000-000000000001', :org_id, :parent_term_id, :slot_id,
            :member100, DATE '2026-06-20', DATE '2026-07-01', 'active'::subscription_assignment_state,
            :owner1
        )
        """,
        base_params,
    )

    await expect_db_error(
        """
        INSERT INTO subscription_slot_assignments (
            id, org_id, term_id, term_slot_id, member_id, effective_from, effective_until,
            assignment_state, assigned_by
        )
        VALUES (
            '81000000-0000-0000-0000-000000000002', :org_id, :parent_term_id, :slot_id,
            :member100, DATE '2026-05-01', DATE '2026-05-31', 'active'::subscription_assignment_state,
            :owner1
        )
        """,
        base_params,
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO subscription_terms (
                    id, org_id, branch_id, series_id, sequence_number, term_code, renewed_from_term_id,
                    source_type, plan_id, plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot,
                    duration_value_snapshot, capacity_snapshot, currency_code, list_price_amount,
                    discount_amount, tax_amount, final_amount, starts_on, base_ends_on, effective_ends_on, status
                )
                VALUES (
                    '80000000-0000-0000-0000-000000000005', :org_id, :branch_id, :series_id, 99,
                    'ADJACENT-OK', :parent_term_id, 'renewal'::subscription_term_source, :plan_id,
                    'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR',
                    3500.00, 0, 0, 3500.00, DATE '2026-09-16', DATE '2026-12-16',
                    DATE '2026-12-16', 'scheduled'::subscription_term_status
                )
                """
            ),
            base_params,
        )
        await session.commit()

    assert await scalar_int("SELECT count(*) FROM subscription_terms WHERE term_code = 'ADJACENT-OK'") == 1

    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM subscription_terms WHERE term_code = 'ADJACENT-OK'"))
        await session.commit()
