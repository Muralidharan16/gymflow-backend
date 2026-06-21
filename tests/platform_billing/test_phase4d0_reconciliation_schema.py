from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import AsyncSessionLocal
from app.platform_billing.models.reconciliation import (
    PlatformReconciliationItem,
    PlatformReconciliationRun,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE4A_REVISION = "014167728f4a"
PHASE4D0_REVISION = "0d4e5f6a7b8c"
PHASE4D0_MIGRATION = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "0d4e5f6a7b8c_add_reconciliation_claim_lifecycle.py"
)

ORG_ID = "8d000000-0000-0000-0000-000000000001"
RUN_ID = "8d000000-0000-0000-0000-000000000101"
ITEM_ID = "8d000000-0000-0000-0000-000000000201"
SHA_A = "a" * 64
EVIDENCE_REF = "phase4d0-evidence://sample"

PHASE4_TABLES = {
    "platform_provider_customers",
    "platform_payment_methods",
    "platform_provider_operations",
    "platform_webhook_inbox",
    "platform_reconciliation_runs",
    "platform_reconciliation_items",
}

RUN_FIELDS = {
    "claim_state",
    "attempt_count",
    "claimed_at",
    "claim_expires_at",
    "updated_at",
    "last_error_code",
    "last_error_at",
}

ITEM_FIELDS = {
    "claim_state",
    "attempt_count",
    "claimed_at",
    "claim_expires_at",
    "updated_at",
    "last_error_code",
    "last_error_at",
}

RUN_CONSTRAINTS = {
    "chk_platform_reconciliation_runs_claim_state",
    "chk_platform_reconciliation_runs_attempt_count",
    "chk_platform_reconciliation_runs_claim_timestamps_paired",
    "chk_platform_reconciliation_runs_processing_claim_metadata",
    "chk_platform_reconciliation_runs_idle_claim_metadata",
    "chk_platform_reconciliation_runs_positive_lease",
    "chk_platform_reconciliation_runs_terminal_idle",
    "chk_platform_reconciliation_runs_error_code_safe",
}

ITEM_CONSTRAINTS = {
    "chk_platform_reconciliation_items_claim_state",
    "chk_platform_reconciliation_items_attempt_count",
    "chk_platform_reconciliation_items_claim_timestamps_paired",
    "chk_platform_reconciliation_items_processing_claim_metadata",
    "chk_platform_reconciliation_items_idle_claim_metadata",
    "chk_platform_reconciliation_items_positive_lease",
    "chk_platform_reconciliation_items_terminal_idle",
    "chk_platform_reconciliation_items_error_code_safe",
}


async def fetch_all(sql: str, params: dict[str, object] | None = None):
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
        result = await session.execute(text(sql), params or {})
        return result.fetchall()


async def fetch_scalar(sql: str, params: dict[str, object] | None = None) -> object:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


async def execute(sql: str, params: dict[str, object] | None = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
        for statement in [part.strip() for part in sql.split(";") if part.strip()]:
            await session.execute(text(statement), params or {})
        await session.commit()


async def expect_db_error(sql: str, params: dict[str, object] | None = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
        with pytest.raises(Exception):
            for statement in [part.strip() for part in sql.split(";") if part.strip()]:
                await session.execute(text(statement), params or {})
            await session.commit()
        await session.rollback()


def run_alembic_on_migration_db(*args: str) -> subprocess.CompletedProcess[str]:
    migration_url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
    if not migration_url:
        pytest.skip("MIGRATION_TEST_DATABASE_URL is required for Phase 4D0 migration-cycle test")
    env = os.environ.copy()
    env["DATABASE_URL"] = migration_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


async def seed_org_run_and_item() -> None:
    await execute(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        DELETE FROM platform_reconciliation_items WHERE id = :item_id;
        DELETE FROM platform_reconciliation_runs WHERE id = :run_id;
        DELETE FROM organizations WHERE id = :org_id;

        INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
        VALUES (:org_id, 'Phase 4D0 Org', 'phase-4d0-org', 'basic'::orgtier, true, 5, 'INR');

        INSERT INTO platform_reconciliation_runs (
            id, provider_code, run_identity, status, scope_json, watermark_json
        )
        VALUES (
            :run_id, 'fake', 'phase4d0-live-run', 'running',
            '{"scope": "phase4d0"}'::jsonb, '{"cursor": "0"}'::jsonb
        );

        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);

        INSERT INTO platform_reconciliation_items (
            id, reconciliation_run_id, organization_id, provider_object_type,
            external_object_ref, local_object_type, local_object_id,
            discrepancy_classification, evidence_sha256, evidence_ref
        )
        VALUES (
            :item_id, :run_id, :org_id, 'provider_operation',
            'fake_op_phase4d0', 'provider_operation', :item_id,
            'local_unknown_provider_succeeded', :sha_a, :evidence_ref
        );
        """,
        {
            "org_id": ORG_ID,
            "run_id": RUN_ID,
            "item_id": ITEM_ID,
            "sha_a": SHA_A,
            "evidence_ref": EVIDENCE_REF,
        },
    )


@pytest.mark.asyncio
async def test_phase4d0_migration_cycle_preserves_existing_rows_and_reverts_schema():
    migration_url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
    if not migration_url:
        pytest.skip("MIGRATION_TEST_DATABASE_URL is required for Phase 4D0 migration-cycle test")

    engine = create_async_engine(migration_url)

    async def scalar(sql: str, params: dict[str, object] | None = None) -> object:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
            result = await conn.execute(text(sql), params or {})
            return result.scalar_one()

    async def exec_migration_sql(sql: str, params: dict[str, object] | None = None) -> None:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
            for statement in [part.strip() for part in sql.split(";") if part.strip()]:
                await conn.execute(text(statement), params or {})

    try:
        run_alembic_on_migration_db("upgrade", "head")
        run_alembic_on_migration_db("downgrade", PHASE4A_REVISION)
        await exec_migration_sql(
            """
            SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
            DELETE FROM platform_reconciliation_items WHERE id = :item_id;
            DELETE FROM platform_reconciliation_runs WHERE id = :run_id;
            DELETE FROM organizations WHERE id = :org_id;

            INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
            VALUES (:org_id, 'Phase 4D0 Migration Org', 'phase-4d0-migration-org', 'basic'::orgtier, true, 5, 'INR');

            INSERT INTO platform_reconciliation_runs (
                id, provider_code, run_identity, status, scope_json, watermark_json
            )
            VALUES (
                :run_id, 'fake', 'phase4d0-migration-run', 'running',
                '{"scope": "migration"}'::jsonb, '{"cursor": "0"}'::jsonb
            );

            SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);

            INSERT INTO platform_reconciliation_items (
                id, reconciliation_run_id, organization_id, provider_object_type,
                external_object_ref, local_object_type, local_object_id,
                discrepancy_classification, evidence_sha256, evidence_ref
            )
            VALUES (
                :item_id, :run_id, :org_id, 'provider_operation',
                'fake_op_phase4d0_migration', 'provider_operation', :item_id,
                'local_unknown_provider_succeeded', :sha_a, :evidence_ref
            );
            """,
            {
                "org_id": ORG_ID,
                "run_id": RUN_ID,
                "item_id": ITEM_ID,
                "sha_a": SHA_A,
                "evidence_ref": EVIDENCE_REF,
            },
        )

        run_alembic_on_migration_db("upgrade", "head")
        assert await scalar("SELECT claim_state FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == "idle"
        assert await scalar("SELECT attempt_count FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == 0
        assert await scalar("SELECT evidence_sha256 FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == SHA_A
        assert await scalar("SELECT evidence_ref FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == EVIDENCE_REF

        run_alembic_on_migration_db("downgrade", PHASE4A_REVISION)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"), {"org_id": ORG_ID})
            result = await conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'platform_reconciliation_items'
                    """
                )
            )
            remaining_columns = {row[0] for row in result}
        assert not (remaining_columns & ITEM_FIELDS)
        assert await scalar("SELECT evidence_sha256 FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == SHA_A
        assert await scalar("SELECT evidence_ref FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == EVIDENCE_REF

        run_alembic_on_migration_db("upgrade", "head")
        assert await scalar("SELECT claim_state FROM platform_reconciliation_items WHERE id = :item_id", {"item_id": ITEM_ID}) == "idle"
    finally:
        run_alembic_on_migration_db("upgrade", "head")
        await exec_migration_sql(
            """
            SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
            DELETE FROM platform_reconciliation_items WHERE id = :item_id;
            DELETE FROM platform_reconciliation_runs WHERE id = :run_id;
            DELETE FROM organizations WHERE id = :org_id;
            """,
            {"org_id": ORG_ID, "run_id": RUN_ID, "item_id": ITEM_ID},
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_phase4d0_schema_columns_constraints_indexes_and_no_new_tables():
    table_names = {
        row[0]
        for row in await fetch_all(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND (
                table_name LIKE 'platform_provider%'
                OR table_name LIKE 'platform_payment%'
                OR table_name LIKE 'platform_webhook%'
                OR table_name LIKE 'platform_reconciliation%'
              )
            """
        )
    }
    assert table_names == PHASE4_TABLES
    assert "platform_provider_subscriptions" not in table_names

    columns = {
        (row[0], row[1])
        for row in await fetch_all(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN ('platform_reconciliation_runs', 'platform_reconciliation_items')
            """
        )
    }
    assert {("platform_reconciliation_runs", field) for field in RUN_FIELDS} <= columns
    assert {("platform_reconciliation_items", field) for field in ITEM_FIELDS} <= columns

    forbidden_columns = {"raw_provider_response", "raw_payload", "payload_json", "provider_secret", "api_key", "database_url"}
    assert not ({column for _table, column in columns} & forbidden_columns)

    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid IN (
                'public.platform_reconciliation_runs'::regclass,
                'public.platform_reconciliation_items'::regclass
            )
            """
        )
    }
    assert RUN_CONSTRAINTS <= constraints
    assert ITEM_CONSTRAINTS <= constraints
    assert "uq_platform_reconciliation_runs_identity" in constraints
    assert "uq_platform_reconciliation_items_run_discrepancy" in constraints

    indexes = {
        row[0]
        for row in await fetch_all(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('platform_reconciliation_runs', 'platform_reconciliation_items')
            """
        )
    }
    assert {
        "ix_platform_reconciliation_runs_claim_recovery",
        "ix_platform_reconciliation_items_claimable",
        "ix_platform_reconciliation_items_stale_processing",
        "ix_platform_reconciliation_items_run_resolution",
    } <= indexes


@pytest.mark.asyncio
async def test_phase4d0_item_claim_lifecycle_constraints():
    await seed_org_run_and_item()

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET attempt_count = -1
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET claim_state = 'processing'
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET claim_state = 'idle',
            claimed_at = clock_timestamp(),
            claim_expires_at = clock_timestamp() + interval '5 minutes'
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET claim_state = 'processing',
            claimed_at = '2026-06-21T00:00:00Z',
            claim_expires_at = '2026-06-21T00:00:00Z'
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET resolution_status = 'resolved',
            resolved_at = clock_timestamp(),
            claim_state = 'processing',
            claimed_at = clock_timestamp(),
            claim_expires_at = clock_timestamp() + interval '5 minutes'
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET last_error_code = 'raw stack trace!'
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )

    await execute(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org_id, true);
        UPDATE platform_reconciliation_items
        SET claim_state = 'processing',
            attempt_count = attempt_count + 1,
            claimed_at = '2026-06-21T00:00:00Z',
            claim_expires_at = '2026-06-21T00:05:00Z',
            last_error_code = 'provider_timeout',
            last_error_at = clock_timestamp()
        WHERE id = :item_id
        """,
        {"org_id": ORG_ID, "item_id": ITEM_ID},
    )
    row = (
        await fetch_all(
            """
            SELECT claim_state, attempt_count, last_error_code
            FROM platform_reconciliation_items
            WHERE id = :item_id
            """,
            {"item_id": ITEM_ID},
        )
    )[0]
    assert tuple(row) == ("processing", 1, "provider_timeout")


@pytest.mark.asyncio
async def test_phase4d0_run_claim_lifecycle_constraints_and_identity():
    await seed_org_run_and_item()

    await expect_db_error(
        "UPDATE platform_reconciliation_runs SET attempt_count = -1 WHERE id = :run_id",
        {"run_id": RUN_ID},
    )
    await expect_db_error(
        "UPDATE platform_reconciliation_runs SET claim_state = 'processing' WHERE id = :run_id",
        {"run_id": RUN_ID},
    )
    await expect_db_error(
        """
        UPDATE platform_reconciliation_runs
        SET claim_state = 'idle',
            claimed_at = clock_timestamp(),
            claim_expires_at = clock_timestamp() + interval '5 minutes'
        WHERE id = :run_id
        """,
        {"run_id": RUN_ID},
    )
    await expect_db_error(
        """
        UPDATE platform_reconciliation_runs
        SET claim_state = 'processing',
            claimed_at = '2026-06-21T00:00:00Z',
            claim_expires_at = '2026-06-21T00:00:00Z'
        WHERE id = :run_id
        """,
        {"run_id": RUN_ID},
    )
    await expect_db_error(
        """
        UPDATE platform_reconciliation_runs
        SET status = 'succeeded',
            completed_at = clock_timestamp(),
            claim_state = 'processing',
            claimed_at = clock_timestamp(),
            claim_expires_at = clock_timestamp() + interval '5 minutes'
        WHERE id = :run_id
        """,
        {"run_id": RUN_ID},
    )
    await expect_db_error(
        """
        INSERT INTO platform_reconciliation_runs (
            provider_code, run_identity, status, scope_json, watermark_json
        )
        VALUES ('fake', 'phase4d0-live-run', 'running', '{}'::jsonb, '{}'::jsonb)
        """,
    )

    await execute(
        """
        UPDATE platform_reconciliation_runs
        SET claim_state = 'processing',
            attempt_count = attempt_count + 1,
            claimed_at = '2026-06-21T00:00:00Z',
            claim_expires_at = '2026-06-21T00:05:00Z',
            last_error_code = 'retryable_reconciliation_error',
            last_error_at = clock_timestamp()
        WHERE id = :run_id
        """,
        {"run_id": RUN_ID},
    )
    row = (
        await fetch_all(
            """
            SELECT claim_state, attempt_count, last_error_code
            FROM platform_reconciliation_runs
            WHERE id = :run_id
            """,
            {"run_id": RUN_ID},
        )
    )[0]
    assert tuple(row) == ("processing", 1, "retryable_reconciliation_error")


@pytest.mark.asyncio
async def test_phase4d0_orm_parity_with_database():
    db_columns = {
        (row[0], row[1]): row[2] == "YES"
        for row in await fetch_all(
            """
            SELECT table_name, column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN ('platform_reconciliation_runs', 'platform_reconciliation_items')
            """
        )
    }
    for model in (PlatformReconciliationRun, PlatformReconciliationItem):
        table = model.__table__
        db_names = {column_name for table_name, column_name in db_columns if table_name == table.name}
        orm_names = {column.name for column in table.columns}
        assert orm_names == db_names
        for column in table.columns:
            if column.primary_key:
                continue
            assert column.nullable is db_columns[table.name, column.name]

    run_constraint_names = {constraint.name for constraint in PlatformReconciliationRun.__table__.constraints}
    item_constraint_names = {constraint.name for constraint in PlatformReconciliationItem.__table__.constraints}
    assert RUN_CONSTRAINTS <= run_constraint_names
    assert ITEM_CONSTRAINTS <= item_constraint_names

    run_index_names = {index.name for index in PlatformReconciliationRun.__table__.indexes}
    item_index_names = {index.name for index in PlatformReconciliationItem.__table__.indexes}
    assert "ix_platform_reconciliation_runs_claim_recovery" in run_index_names
    assert {
        "ix_platform_reconciliation_items_claimable",
        "ix_platform_reconciliation_items_stale_processing",
        "ix_platform_reconciliation_items_run_resolution",
    } <= item_index_names


@pytest.mark.asyncio
async def test_phase4d0_security_and_scope_guardrails():
    rls = {
        row[0]: (row[1], row[2])
        for row in await fetch_all(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relnamespace = 'public'::regnamespace
              AND relname = 'platform_reconciliation_items'
            """
        )
    }
    assert rls == {"platform_reconciliation_items": (True, True)}

    policies = {
        row[0]
        for row in await fetch_all(
            """
            SELECT policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = 'platform_reconciliation_items'
            """
        )
    }
    assert "tenant_isolation_platform_reconciliation_items" in policies

    public_mutations = await fetch_scalar(
        """
        SELECT count(*)
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name IN ('platform_reconciliation_runs', 'platform_reconciliation_items')
          AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
        """
    )
    assert public_mutations == 0

    source = PHASE4D0_MIGRATION.read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = {
        "create role",
        "alter role",
        "bypassrls",
        "superuser",
        "database_url",
        "postgres://",
        "api_key",
        "secret_key",
        "raw_provider_response",
        "raw_payload",
        "payload_json",
        "provider evidence reader",
        "requests.",
        "httpx.",
        "razorpay",
        "cashfree",
        "stripe",
    }
    for token in forbidden:
        assert token not in lowered

    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "checkout.py").exists()
