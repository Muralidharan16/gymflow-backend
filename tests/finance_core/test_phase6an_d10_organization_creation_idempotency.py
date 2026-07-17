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


REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "1a2b3c4d5e7f"
D10_REVISION = "2b3c4d5e6f70"
ORG_ID = uuid.UUID("6d100000-0000-0000-0000-000000000010")
SHA = "a" * 64


async def fetch_all(sql: str, params: dict[str, object] | None = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.fetchall()


async def fetch_scalar(sql: str, params: dict[str, object] | None = None) -> object:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


async def expect_db_error(sql: str, params: dict[str, object] | None = None) -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception):
            await session.execute(text(sql), params or {})
            await session.commit()
        await session.rollback()


def run_alembic_on_migration_db(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    migration_url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
    if not migration_url:
        pytest.skip("MIGRATION_TEST_DATABASE_URL is required for D10 migration-cycle tests")
    env = os.environ.copy()
    env["DATABASE_URL"] = migration_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


@pytest.mark.asyncio
async def test_phase6an_d10_table_columns_constraints_and_grants_exist():
    columns = {row[0]: row for row in await fetch_all(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'organization_creation_idempotency'
        """
    )}
    assert set(columns) == {
        "id",
        "operation",
        "idempotency_key",
        "request_hash_sha256",
        "canonicalization_version",
        "organization_id",
        "trusted_source",
        "created_at",
        "completed_at",
    }
    assert all(row[2] == "NO" for row in columns.values())

    constraints = {row[0] for row in await fetch_all(
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.organization_creation_idempotency'::regclass
        """
    )}
    assert {
        "organization_creation_idempotency_pkey",
        "uq_org_creation_idem_operation_key",
        "uq_org_creation_idem_operation_org",
        "fk_org_creation_idem_organization",
        "chk_org_creation_idem_operation",
        "chk_org_creation_idem_key_format",
        "chk_org_creation_idem_request_hash",
        "chk_org_creation_idem_canonical_version",
        "chk_org_creation_idem_trusted_source",
        "chk_org_creation_idem_completed_after_created",
    } <= constraints

    fk_delete = await fetch_scalar(
        """
        SELECT confdeltype
        FROM pg_constraint
        WHERE conname = 'fk_org_creation_idem_organization'
          AND conrelid = 'public.organization_creation_idempotency'::regclass
        """
    )
    assert fk_delete in {"r", b"r"}
    public_privs = await fetch_all(
        """
        SELECT privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'organization_creation_idempotency'
          AND grantee = 'PUBLIC'
        """
    )
    assert public_privs == []


@pytest.mark.asyncio
async def test_phase6an_d10_immutability_and_constraints_reject_mutation_inside_rollback():
    async with AsyncSessionLocal() as session:
        tx = await session.begin()
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES (:org_id, 'D10 TEST Migration Org', 'd10-test-migration-org', 'basic', true, 1, 'INR')
                """
            ),
            {"org_id": ORG_ID},
        )
        await session.execute(
            text(
                """
                INSERT INTO organization_creation_idempotency (
                    operation, idempotency_key, request_hash_sha256, canonicalization_version,
                    organization_id, trusted_source
                ) VALUES (
                    'synthetic_organization_create', 'organization-create:synthetic:test:d10-migration',
                    :sha, 1, :org_id, 'finance_razorpay_test_precondition'
                )
                """
            ),
            {"sha": SHA, "org_id": ORG_ID},
        )
        with pytest.raises(Exception):
            await session.execute(
                text("UPDATE organization_creation_idempotency SET trusted_source = trusted_source WHERE organization_id = :org_id"),
                {"org_id": ORG_ID},
            )
        await tx.rollback()

    await expect_db_error(
        """
        INSERT INTO organization_creation_idempotency (
            operation, idempotency_key, request_hash_sha256, canonicalization_version,
            organization_id, trusted_source
        ) VALUES ('other', 'organization-create:synthetic:test:bad', :sha, 1, :org_id, 'finance_razorpay_test_precondition')
        """,
        {"sha": SHA, "org_id": ORG_ID},
    )


@pytest.mark.asyncio
async def test_phase6an_d10_empty_migration_cycle_on_migration_database():
    run_alembic_on_migration_db("upgrade", D10_REVISION)
    run_alembic_on_migration_db("downgrade", PREVIOUS_REVISION)
    run_alembic_on_migration_db("upgrade", D10_REVISION)
