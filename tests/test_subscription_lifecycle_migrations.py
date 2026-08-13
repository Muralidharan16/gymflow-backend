from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from conftest import APP_DATABASE_URL, TEST_DATABASE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "b1c2d3e4f5a6"
CONSTRAINT_REVISION = "e5f6a7b8c9d0"


def _validated_migration_test_database_url() -> str:
    if not APP_DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for lifecycle migration tests")
    migration_url = make_url(APP_DATABASE_URL)
    runtime_url = make_url(TEST_DATABASE_URL)
    migration_db = migration_url.database or ""
    runtime_db = runtime_url.database or ""
    if "test" not in migration_db.lower():
        raise RuntimeError(
            "Lifecycle migration tests require an unmistakably disposable migration database; "
            f"got {migration_db!r}."
        )
    if migration_db == runtime_db:
        raise RuntimeError(
            "Lifecycle migration rehearsals must not mutate the General runtime test database; "
            f"both URLs target {migration_db!r}."
        )
    return APP_DATABASE_URL


MIGRATION_TEST_DATABASE_URL = _validated_migration_test_database_url()
migration_test_async_engine = create_async_engine(
    MIGRATION_TEST_DATABASE_URL,
    poolclass=NullPool,
    pool_pre_ping=True,
    echo=False,
)
MigrationTestSessionLocal = async_sessionmaker(
    migration_test_async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@dataclass(frozen=True)
class LifecycleSeed:
    suffix: str
    org_1: str
    org_2: str
    owner_1: str
    owner_2: str
    branch_1: str
    branch_2: str
    branch_3: str
    member_100: str
    member_101: str
    member_200: str
    plan_individual: str
    plan_family: str
    plan_other: str
    sub_a: str
    sub_b: str
    sub_family: str
    sub_expired: str


def new_seed() -> LifecycleSeed:
    return LifecycleSeed(
        uuid.uuid4().hex[:10],
        *(str(uuid.uuid4()) for _ in range(17)),
    )


def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = MIGRATION_TEST_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Alembic lifecycle test command failed: "
            f"{' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


async def _set_tenant_context(session: AsyncSession, org_id: str, owner_id: str) -> None:
    """Seed through the same fail-closed tenant boundary enforced by baseline RLS."""
    settings = {
        "app.current_org_id": org_id,
        "app.current_user_id": owner_id,
        "app.current_principal_id": owner_id,
        "app.current_principal_type": "owner",
        "app.current_role": "owner",
    }
    for key, value in settings.items():
        await session.execute(
            text("SELECT pg_catalog.set_config(:key, :value, true)"),
            {"key": key, "value": value},
        )


async def seed_v2_source_data(seed: LifecycleSeed) -> None:
    member_base = int(seed.suffix[:7], 16) % 8_000_000 + 1_000_000
    phone_base = int(seed.suffix[:8], 16) % 800_000_000 + 100_000_000
    params = {
        "org1": seed.org_1,
        "org2": seed.org_2,
        "owner1": seed.owner_1,
        "owner2": seed.owner_2,
        "branch1": seed.branch_1,
        "branch2": seed.branch_2,
        "branch3": seed.branch_3,
        "member100": seed.member_100,
        "member101": seed.member_101,
        "member200": seed.member_200,
        "planIndividual": seed.plan_individual,
        "planFamily": seed.plan_family,
        "planOther": seed.plan_other,
        "subA": seed.sub_a,
        "subB": seed.sub_b,
        "subFamily": seed.sub_family,
        "subExpired": seed.sub_expired,
        "suffix": seed.suffix,
        "memberNo100": member_base,
        "memberNo101": member_base + 1,
        "memberNo200": member_base + 2,
        "phone100": f"9{phone_base:09d}",
        "phone101": f"8{phone_base:09d}",
        "phone200": f"7{phone_base:09d}",
    }

    async with MigrationTestSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES
                  (:org1, 'Lifecycle Org 1 ' || :suffix, 'lifecycle-org-1-' || :suffix, 'basic'::orgtier, true, 5, 'INR'),
                  (:org2, 'Lifecycle Org 2 ' || :suffix, 'lifecycle-org-2-' || :suffix, 'basic'::orgtier, true, 5, 'INR')
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                INSERT INTO owners (id, org_id, owner_name, email, hashed_password, email_verified)
                VALUES
                  (:owner1, :org1, 'Owner One', 'lifecycle-owner-1+' || :suffix || '@test.local', 'hash', true),
                  (:owner2, :org2, 'Owner Two', 'lifecycle-owner-2+' || :suffix || '@test.local', 'hash', true)
                """
            ),
            params,
        )

        await _set_tenant_context(session, seed.org_1, seed.owner_1)
        for statement in (
            """
            INSERT INTO org_branches (id, org_id, branch_name, branch_code, internal_slug, timezone, currency_code, country_code, created_by)
            VALUES
              (:branch1, :org1, 'Lifecycle Main', 'MAIN', 'lifecycle-main-' || :suffix, 'Asia/Kolkata', 'INR', 'IN', :owner1),
              (:branch2, :org1, 'Lifecycle Annex', 'ANNEX', 'lifecycle-annex-' || :suffix, 'Asia/Kolkata', 'INR', 'IN', :owner1)
            """,
            """
            INSERT INTO members (id, org_id, home_branch_id, member_uid, member_number, name, phone, status, is_active, is_migrated)
            VALUES
              (:member100, :org1, :branch1, 'LIFE-100-' || :suffix, :memberNo100, 'Member One Hundred', :phone100, 'active'::memberstatus, true, false),
              (:member101, :org1, :branch1, 'LIFE-101-' || :suffix, :memberNo101, 'Member One Hundred One', :phone101, 'active'::memberstatus, true, false)
            """,
            """
            INSERT INTO membership_plans (
              id, org_id, branch_id, plan_code, name, price, currency, duration_value, duration_unit, max_members, status
            ) VALUES
              (:planIndividual, :org1, :branch1, 'IND-QUARTER', 'Individual Quarterly', 3500.00, 'INR', 3, 'months'::duration_unit, 1, 'active'::plan_status),
              (:planFamily, :org1, :branch1, 'FAM-QUARTER', 'Family Quarterly', 4000.00, 'INR', 3, 'months'::duration_unit, 3, 'active'::plan_status)
            """,
            """
            INSERT INTO member_subscriptions_v2 (
              id, org_id, branch_id, membership_plan_id, primary_member_id, subscription_code,
              start_date, end_date, status, price_snapshot, currency_code, duration_value_snapshot,
              duration_unit_snapshot, max_members_snapshot, created_by, updated_by, created_at, updated_at
            ) VALUES
              (:subA, :org1, :branch1, :planIndividual, :member100, 'SUB-ADJ-A-' || :suffix,
               DATE '2026-06-15', DATE '2026-09-15', 'active'::modern_subscription_status,
               3500.00, 'INR', 3, 'months'::duration_unit, 1, :owner1, :owner1, now(), now()),
              (:subB, :org1, :branch1, :planIndividual, :member100, 'SUB-ADJ-B-' || :suffix,
               DATE '2026-09-16', DATE '2026-12-16', 'active'::modern_subscription_status,
               3500.00, 'INR', 3, 'months'::duration_unit, 1, :owner1, :owner1, now(), now()),
              (:subFamily, :org1, :branch1, :planFamily, :member101, 'SUB-FAM-' || :suffix,
               DATE '2026-06-15', DATE '2026-09-15', 'active'::modern_subscription_status,
               4000.00, 'INR', 3, 'months'::duration_unit, 3, :owner1, :owner1, now(), now())
            """,
            """
            INSERT INTO subscription_members (
              id, org_id, subscription_id, member_id, slot_number, role, is_active, joined_at, created_at, updated_at
            ) VALUES
              (gen_random_uuid(), :org1, :subA, :member100, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
              (gen_random_uuid(), :org1, :subB, :member100, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
              (gen_random_uuid(), :org1, :subFamily, :member101, 1, 'primary'::subscription_member_role, true, now(), now(), now()),
              (gen_random_uuid(), :org1, :subFamily, :member100, 2, 'additional'::subscription_member_role, true, now(), now(), now())
            """,
        ):
            await session.execute(text(statement), params)

        await _set_tenant_context(session, seed.org_2, seed.owner_2)
        for statement in (
            """
            INSERT INTO org_branches (id, org_id, branch_name, branch_code, internal_slug, timezone, currency_code, country_code, created_by)
            VALUES (:branch3, :org2, 'Lifecycle Other', 'OTHER', 'lifecycle-other-' || :suffix, 'Asia/Kolkata', 'INR', 'IN', :owner2)
            """,
            """
            INSERT INTO members (id, org_id, home_branch_id, member_uid, member_number, name, phone, status, is_active, is_migrated)
            VALUES (:member200, :org2, :branch3, 'LIFE-200-' || :suffix, :memberNo200, 'Member Two Hundred', :phone200, 'active'::memberstatus, true, false)
            """,
            """
            INSERT INTO membership_plans (
              id, org_id, branch_id, plan_code, name, price, currency, duration_value, duration_unit, max_members, status
            ) VALUES (:planOther, :org2, :branch3, 'OTHER-MONTH', 'Other Monthly', 1200.00, 'INR', 1, 'months'::duration_unit, 1, 'active'::plan_status)
            """,
            """
            INSERT INTO member_subscriptions_v2 (
              id, org_id, branch_id, membership_plan_id, primary_member_id, subscription_code,
              start_date, end_date, status, price_snapshot, currency_code, duration_value_snapshot,
              duration_unit_snapshot, max_members_snapshot, created_by, updated_by, created_at, updated_at
            ) VALUES (
              :subExpired, :org2, :branch3, :planOther, :member200, 'SUB-EXP-' || :suffix,
              DATE '2025-01-01', DATE '2025-02-01', 'expired'::modern_subscription_status,
              1200.00, 'INR', 1, 'months'::duration_unit, 1, :owner2, :owner2, now(), now()
            )
            """,
            """
            INSERT INTO subscription_members (
              id, org_id, subscription_id, member_id, slot_number, role, is_active, joined_at, created_at, updated_at
            ) VALUES (gen_random_uuid(), :org2, :subExpired, :member200, 1, 'primary'::subscription_member_role, true, now(), now(), now())
            """,
        ):
            await session.execute(text(statement), params)

        await session.commit()


async def scalar_int(sql: str, params: dict[str, object] | None = None) -> int:
    async with MigrationTestSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return int(result.scalar_one())


async def assert_table_absent(table_name: str) -> None:
    assert await scalar_int(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name=:table_name",
        {"table_name": table_name},
    ) == 0


async def expect_db_error(
    sql: str,
    params: dict[str, object],
    expected_fragment: str,
) -> None:
    async with MigrationTestSessionLocal() as session:
        with pytest.raises(Exception) as exc_info:
            await session.execute(text(sql), params)
            await session.commit()
        await session.rollback()
    assert expected_fragment in str(exc_info.value), (
        f"expected database failure containing {expected_fragment!r}, "
        f"got {exc_info.value!r}"
    )


def _subscription_params(seed: LifecycleSeed) -> dict[str, object]:
    return {
        "sub_a": seed.sub_a,
        "sub_b": seed.sub_b,
        "sub_family": seed.sub_family,
        "sub_expired": seed.sub_expired,
    }


async def assert_baseline_source_preserved(seed: LifecycleSeed) -> None:
    """Verify exact source rows while the schema is at the pre-lifecycle baseline.

    Current head deliberately FORCE-enables tenant RLS on the legacy member
    subscription tables and scopes those policies to app_runtime. The reduced
    migration_owner therefore must not be used as a backdoor to read those
    source rows after head. Preservation is proved at the predecessor, after
    downgrade, and by the lifecycle rows produced at head.
    """
    params = _subscription_params(seed)
    id_filter = "(:sub_a, :sub_b, :sub_family, :sub_expired)"
    assert await scalar_int(
        f"SELECT count(*) FROM member_subscriptions_v2 WHERE id IN {id_filter}",
        params,
    ) == 4
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_members WHERE subscription_id IN {id_filter}",
        params,
    ) == 5


async def prepare_migrated_lifecycle(target_revision: str = "head") -> LifecycleSeed:
    run_alembic("upgrade", "head")
    run_alembic("downgrade", BASELINE_REVISION)
    seed = new_seed()
    await seed_v2_source_data(seed)
    await assert_baseline_source_preserved(seed)
    run_alembic("upgrade", target_revision)
    return seed


async def test_lifecycle_migration_round_trip_preserves_v2_and_backfills_conservatively():
    seed = await prepare_migrated_lifecycle()
    params = _subscription_params(seed)
    id_filter = "(:sub_a, :sub_b, :sub_family, :sub_expired)"
    series_count_sql = f"""
        SELECT count(DISTINCT s.id)
        FROM subscription_series AS s
        JOIN subscription_terms AS t ON t.series_id = s.id
        WHERE t.legacy_member_subscription_v2_id IN {id_filter}
    """

    # At current head member_subscriptions_v2 is FORCE-RLS and its policies are
    # scoped to app_runtime. Migration-owner verification therefore starts with
    # the lifecycle rows produced from the already-proven baseline source set.
    assert await scalar_int(series_count_sql, params) == 4
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_terms WHERE legacy_member_subscription_v2_id IN {id_filter}",
        params,
    ) == 4
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_term_slots s JOIN subscription_terms t ON t.id=s.term_id WHERE t.legacy_member_subscription_v2_id IN {id_filter}",
        params,
    ) == 6
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_slot_assignments a JOIN subscription_terms t ON t.id=a.term_id WHERE t.legacy_member_subscription_v2_id IN {id_filter}",
        params,
    ) == 5
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_terms WHERE legacy_member_subscription_v2_id IN {id_filter} AND renewed_from_term_id IS NOT NULL",
        params,
    ) == 0
    assert await scalar_int(
        "SELECT count(DISTINCT series_id) FROM subscription_terms WHERE legacy_member_subscription_v2_id IN (:sub_a,:sub_b)",
        params,
    ) == 2

    async with MigrationTestSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT term_code, legacy_subscription_code, plan_code_snapshot, plan_name_snapshot,
                           capacity_snapshot, list_price_amount, final_amount
                    FROM subscription_terms WHERE legacy_member_subscription_v2_id=:sub_family
                    """
                ),
                {"sub_family": seed.sub_family},
            )
        ).one()
    assert row.term_code == f"SUB-FAM-{seed.suffix}"
    assert row.legacy_subscription_code == f"SUB-FAM-{seed.suffix}"
    assert row.plan_code_snapshot == "FAM-QUARTER"
    assert row.plan_name_snapshot == "Family Quarterly"
    assert row.capacity_snapshot == 3
    assert str(row.list_price_amount) == "4000.00"
    assert str(row.final_amount) == "4000.00"

    assert await scalar_int(
        """
        SELECT count(*) FROM subscription_term_slots s JOIN subscription_terms t ON t.id=s.term_id
        WHERE t.legacy_member_subscription_v2_id=:sub_family
        """,
        {"sub_family": seed.sub_family},
    ) == 3
    assert await scalar_int(
        """
        SELECT count(*) FROM subscription_slot_assignments a JOIN subscription_terms t ON t.id=a.term_id
        WHERE t.legacy_member_subscription_v2_id=:sub_family
        """,
        {"sub_family": seed.sub_family},
    ) == 2

    run_alembic("downgrade", BASELINE_REVISION)
    await assert_table_absent("subscription_series")
    await assert_baseline_source_preserved(seed)

    run_alembic("upgrade", "head")
    assert await scalar_int(series_count_sql, params) == 4
    assert await scalar_int(
        f"SELECT count(*) FROM subscription_terms WHERE legacy_member_subscription_v2_id IN {id_filter}",
        params,
    ) == 4


async def _assert_lifecycle_constraints(seed: LifecycleSeed) -> None:
    async with MigrationTestSessionLocal() as session:
        term = (
            await session.execute(
                text(
                    """
                    SELECT id, org_id, branch_id, series_id, plan_id
                    FROM subscription_terms WHERE legacy_member_subscription_v2_id=:sub_a
                    """
                ),
                {"sub_a": seed.sub_a},
            )
        ).mappings().one()
        other_series = (
            await session.execute(
                text("SELECT series_id FROM subscription_terms WHERE legacy_member_subscription_v2_id=:sub_family"),
                {"sub_family": seed.sub_family},
            )
        ).scalar_one()
        slot_id = (
            await session.execute(
                text("SELECT id FROM subscription_term_slots WHERE term_id=:term_id AND slot_index=1"),
                {"term_id": term["id"]},
            )
        ).scalar_one()

    base = {
        "org_id": term["org_id"],
        "branch_id": term["branch_id"],
        "series_id": term["series_id"],
        "plan_id": term["plan_id"],
        "parent_term_id": term["id"],
        "other_series_id": other_series,
        "slot_id": slot_id,
        "member100": seed.member_100,
        "owner1": seed.owner_1,
    }
    term_prefix = """
        INSERT INTO subscription_terms (
          id, org_id, branch_id, series_id, sequence_number, term_code, source_type, plan_id,
          plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
          capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
          starts_on, base_ends_on, effective_ends_on, status
        ) VALUES
    """
    await expect_db_error(
        term_prefix + """
        (gen_random_uuid(), :org_id, :branch_id, :series_id, 99, 'BAD-DATES', 'admin_adjustment'::subscription_term_source,
         :plan_id, 'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR', 3500,0,0,3500,
         DATE '2026-10-01', DATE '2026-09-01', DATE '2026-09-01', 'scheduled'::subscription_term_status)
        """,
        base,
        "chk_subscription_terms_dates_order",
    )
    await expect_db_error(
        term_prefix + """
        (gen_random_uuid(), :org_id, :branch_id, :series_id, 99, 'BAD-AMOUNT', 'admin_adjustment'::subscription_term_source,
         :plan_id, 'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR', -1,0,0,3500,
         DATE '2026-10-01', DATE '2026-12-01', DATE '2026-12-01', 'scheduled'::subscription_term_status)
        """,
        base,
        "chk_subscription_terms_amounts_nonnegative",
    )
    await expect_db_error(
        term_prefix + """
        (gen_random_uuid(), :org_id, :branch_id, :series_id, 99, 'OVERLAP', 'admin_adjustment'::subscription_term_source,
         :plan_id, 'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit, 3, 1, 'INR', 3500,0,0,3500,
         DATE '2026-07-01', DATE '2026-08-01', DATE '2026-08-01', 'scheduled'::subscription_term_status)
        """,
        base,
        "ex_subscription_terms_series_reserving_overlap",
    )
    await expect_db_error(
        """
        INSERT INTO subscription_terms (
          id, org_id, branch_id, series_id, sequence_number, term_code, renewed_from_term_id, source_type,
          plan_id, plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
          capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
          starts_on, base_ends_on, effective_ends_on, status
        ) VALUES (
          gen_random_uuid(), :org_id, :branch_id, :other_series_id, 99, 'CROSS-LINEAGE', :parent_term_id,
          'renewal'::subscription_term_source, :plan_id, 'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit,
          3,1,'INR',3500,0,0,3500, DATE '2026-10-01', DATE '2026-12-01', DATE '2026-12-01',
          'scheduled'::subscription_term_status)
        """,
        base,
        "subscription_terms renewal parent must belong to same subscription series",
    )
    assignment_prefix = """
        INSERT INTO subscription_slot_assignments (
          id, org_id, term_id, term_slot_id, member_id, effective_from, effective_until, assignment_state, assigned_by
        ) VALUES
    """
    await expect_db_error(
        assignment_prefix + """
        (gen_random_uuid(), :org_id, :parent_term_id, :slot_id, :member100,
         DATE '2026-06-20', DATE '2026-07-01', 'active'::subscription_assignment_state, :owner1)
        """,
        base,
        "ex_subscription_slot_assignments_slot_overlap",
    )
    await expect_db_error(
        assignment_prefix + """
        (gen_random_uuid(), :org_id, :parent_term_id, :slot_id, :member100,
         DATE '2026-05-01', DATE '2026-05-31', 'active'::subscription_assignment_state, :owner1)
        """,
        base,
        "subscription_slot_assignments dates must fit within subscription term",
    )

    adjacent_code = f"ADJACENT-OK-{seed.suffix}"
    async with MigrationTestSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO subscription_terms (
                  id, org_id, branch_id, series_id, sequence_number, term_code, renewed_from_term_id, source_type,
                  plan_id, plan_code_snapshot, plan_name_snapshot, duration_unit_snapshot, duration_value_snapshot,
                  capacity_snapshot, currency_code, list_price_amount, discount_amount, tax_amount, final_amount,
                  starts_on, base_ends_on, effective_ends_on, status
                ) VALUES (
                  gen_random_uuid(), :org_id, :branch_id, :series_id, 99, :adjacent_code, :parent_term_id,
                  'renewal'::subscription_term_source, :plan_id, 'IND-QUARTER', 'Individual Quarterly', 'months'::duration_unit,
                  3,1,'INR',3500,0,0,3500, DATE '2026-09-16', DATE '2026-12-16', DATE '2026-12-16',
                  'scheduled'::subscription_term_status)
                """
            ),
            {**base, "adjacent_code": adjacent_code},
        )
        await session.commit()

    assert await scalar_int(
        "SELECT count(*) FROM subscription_terms WHERE term_code=:code",
        {"code": adjacent_code},
    ) == 1
    async with MigrationTestSessionLocal() as session:
        await session.execute(
            text("DELETE FROM subscription_terms WHERE term_code=:code"),
            {"code": adjacent_code},
        )
        await session.commit()


async def test_lifecycle_constraints_enforce_overlap_tenant_lineage_and_assignment_integrity():
    seed = await prepare_migrated_lifecycle(CONSTRAINT_REVISION)
    try:
        await _assert_lifecycle_constraints(seed)
    finally:
        # Later revisions intentionally FORCE-RLS the legacy member/plan tables
        # and scope those policies to app_runtime. Restore the dedicated
        # migration database to current head without granting migration_owner a
        # runtime visibility path just to execute this historical contract test.
        run_alembic("upgrade", "head")
