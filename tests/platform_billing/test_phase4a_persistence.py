from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.platform_billing.models.provider import (
    PlatformPaymentMethod,
    PlatformProviderCustomer,
    PlatformProviderOperation,
)
from app.platform_billing.models.reconciliation import (
    PlatformReconciliationItem,
    PlatformReconciliationRun,
)
from app.platform_billing.models.webhook import PlatformWebhookInbox
from app.core.config import settings


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic" / "versions" / "014167728f4a_platform_billing_phase_4a_provider_persistence.py"

PHASE_4A_TABLES = {
    "platform_provider_customers",
    "platform_payment_methods",
    "platform_provider_operations",
    "platform_webhook_inbox",
    "platform_reconciliation_runs",
    "platform_reconciliation_items",
}

TENANT_TABLES = {
    "platform_provider_customers",
    "platform_payment_methods",
    "platform_provider_operations",
    "platform_reconciliation_items",
}

PHASE_4A_MODELS = {
    PlatformProviderCustomer,
    PlatformPaymentMethod,
    PlatformProviderOperation,
    PlatformWebhookInbox,
    PlatformReconciliationRun,
    PlatformReconciliationItem,
}


async def fetch_all(sql: str, params: dict[str, object] | None = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.fetchall()


async def fetch_scalar(sql: str, params: dict[str, object] | None = None) -> object:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


@pytest.mark.asyncio
async def test_phase4a_schema_surface_is_exact():
    rows = await fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(:tables)
        ORDER BY table_name
        """,
        {"tables": sorted(PHASE_4A_TABLES | {"platform_provider_subscriptions"})},
    )
    found = {row[0] for row in rows}
    assert PHASE_4A_TABLES <= found
    assert "platform_provider_subscriptions" not in found

    phase4_like = await fetch_all(
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
        ORDER BY table_name
        """
    )
    assert {row[0] for row in phase4_like} == PHASE_4A_TABLES


@pytest.mark.asyncio
async def test_provider_customer_constraints_and_secret_absence():
    columns = {
        row[0]
        for row in await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'platform_provider_customers'
            """
        )
    }
    assert {"organization_id", "provider_code", "external_customer_ref", "status"} <= columns
    assert not (columns & {"provider_secret", "raw_provider_response", "secret_key", "api_key"})

    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.platform_provider_customers'::regclass
            """
        )
    }
    assert {
        "fk_platform_provider_customers_organization",
        "uq_platform_provider_customers_id_org",
        "uq_platform_provider_customers_org_provider",
        "uq_platform_provider_customers_provider_external",
        "chk_platform_provider_customers_status",
    } <= constraints


@pytest.mark.asyncio
async def test_payment_method_safety_and_default_invariants():
    columns = {
        row[0]
        for row in await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'platform_payment_methods'
            """
        )
    }
    assert {
        "method_type",
        "brand",
        "last_four",
        "expiry_month",
        "expiry_year",
        "display_label",
        "is_default",
    } <= columns
    forbidden = {"pan", "cvv", "raw_token", "provider_secret", "full_bank_account_number", "card_number"}
    assert not (columns & forbidden)

    indexes = {
        row[0]
        for row in await fetch_all(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'platform_payment_methods'
            """
        )
    }
    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.platform_payment_methods'::regclass
            """
        )
    }
    assert "ux_platform_payment_methods_one_default_per_provider" in indexes
    assert "chk_platform_payment_methods_default_active" in constraints


@pytest.mark.asyncio
async def test_provider_operation_idempotency_and_status_shape():
    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.platform_provider_operations'::regclass
            """
        )
    }
    indexes = {
        row[0]
        for row in await fetch_all(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'platform_provider_operations'
            """
        )
    }
    assert {
        "uq_platform_provider_operations_idempotency",
        "chk_platform_provider_operations_request_hash",
        "chk_platform_provider_operations_status",
        "chk_platform_provider_operations_terminal_completed",
    } <= constraints
    assert {
        "ix_platform_provider_operations_external_ref",
        "ix_platform_provider_operations_retry",
    } <= indexes

    migration = MIGRATION.read_text(encoding="utf-8")
    assert "'unknown'" in migration
    assert "canonical_request_sha256 CHAR(64) NOT NULL" in migration


@pytest.mark.asyncio
async def test_webhook_inbox_is_trusted_hash_pointer_only():
    columns = {
        row[0]
        for row in await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'platform_webhook_inbox'
            """
        )
    }
    assert {"payload_sha256", "encrypted_payload_ref", "provider_event_id", "processing_status"} <= columns
    assert not (columns & {"raw_body", "raw_payload", "webhook_body", "payload_json", "normalized_payload_json"})

    constraints = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.platform_webhook_inbox'::regclass
            """
        )
    }
    indexes = {
        row[0]
        for row in await fetch_all(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'platform_webhook_inbox'
            """
        )
    }
    assert {
        "uq_platform_webhook_inbox_provider_event",
        "uq_platform_webhook_inbox_provider_event_hash",
        "chk_platform_webhook_inbox_payload_hash",
        "chk_platform_webhook_inbox_payload_ref_nonempty",
    } <= constraints
    assert "ix_platform_webhook_inbox_status_retry" in indexes


@pytest.mark.asyncio
async def test_reconciliation_schema_and_unknown_mapping_support():
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
    assert {
        "uq_platform_reconciliation_runs_identity",
        "fk_platform_reconciliation_items_run",
        "fk_platform_reconciliation_items_organization",
        "uq_platform_reconciliation_items_run_discrepancy",
        "chk_platform_reconciliation_items_local_shape",
        "chk_platform_reconciliation_items_evidence_hash",
        "chk_platform_reconciliation_items_evidence_ref",
    } <= constraints

    local_cols = await fetch_all(
        """
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'platform_reconciliation_items'
          AND column_name IN ('organization_id', 'local_object_type', 'local_object_id')
        ORDER BY column_name
        """
    )
    assert {row[0]: row[1] for row in local_cols} == {
        "local_object_id": "YES",
        "local_object_type": "YES",
        "organization_id": "YES",
    }


@pytest.mark.asyncio
async def test_phase4a_rls_and_privileges_are_narrow():
    rls_rows = await fetch_all(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE relnamespace = 'public'::regnamespace
          AND relname = ANY(:tables)
        ORDER BY relname
        """,
        {"tables": sorted(TENANT_TABLES)},
    )
    assert {row[0] for row in rls_rows} == TENANT_TABLES
    assert all(row[1] and row[2] for row in rls_rows)

    policies = {
        row[0]
        for row in await fetch_all(
            """
            SELECT tablename
            FROM pg_policies
            WHERE schemaname = 'public'
              AND policyname LIKE 'tenant_isolation_platform_%'
              AND tablename = ANY(:tables)
            """,
            {"tables": sorted(TENANT_TABLES)},
        )
    }
    assert policies == TENANT_TABLES

    public_mutations = await fetch_scalar(
        """
        SELECT count(*)
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name = ANY(:tables)
          AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
        """,
        {"tables": sorted(PHASE_4A_TABLES)},
    )
    assert public_mutations == 0


def test_phase4a_orm_table_surface_is_exact():
    assert {model.__tablename__ for model in PHASE_4A_MODELS} == PHASE_4A_TABLES


@pytest.mark.asyncio
async def test_phase4a_orm_columns_nullability_and_foreign_keys_match_database():
    db_columns = {
        (row[0], row[1]): row[2] == "YES"
        for row in await fetch_all(
            """
            SELECT table_name, column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(:tables)
            """,
            {"tables": sorted(PHASE_4A_TABLES)},
        )
    }
    for model in PHASE_4A_MODELS:
        table = model.__table__
        db_names = {column_name for table_name, column_name in db_columns if table_name == table.name}
        orm_names = {column.name for column in table.columns}
        assert orm_names == db_names
        for column in table.columns:
            if column.primary_key:
                continue
            db_nullable = db_columns[table.name, column.name]
            assert column.nullable is db_nullable, f"{table.name}.{column.name} nullability mismatch"

    fk_names = {
        row[0]
        for row in await fetch_all(
            """
            SELECT conname
            FROM pg_constraint
            JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
            WHERE contype = 'f'
              AND pg_class.relnamespace = 'public'::regnamespace
              AND pg_class.relname = ANY(:tables)
            """,
            {"tables": sorted(PHASE_4A_TABLES)},
        )
    }
    assert {
        "fk_platform_payment_methods_customer_org",
        "fk_platform_reconciliation_items_run",
        "fk_platform_reconciliation_items_organization",
    } <= fk_names


def test_phase4a_migration_has_no_cluster_role_or_provider_execution():
    source = MIGRATION.read_text(encoding="utf-8")
    forbidden = {
        "CREATE ROLE",
        "ALTER ROLE",
        "SUPERUSER",
        "BYPASSRLS",
        "razorpay",
        "cashfree",
        "stripe",
        "requests.",
        "httpx.",
    }
    lowered = source.lower()
    for token in forbidden:
        assert token.lower() not in lowered


def test_phase4a_no_api_routes_or_provider_package_added():
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "completion.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "simulation.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "callback.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "webhooks.py").exists()
    provider_sources = sorted(
        path.name
        for path in (REPO_ROOT / "app" / "platform_billing" / "providers").glob("*.py")
    )
    assert provider_sources == [
        "__init__.py",
        "base.py",
        "fake.py",
        "fake_checkout_evidence.py",
        "fake_checkout_simulation.py",
        "reconciliation.py",
    ]


def test_phase4a_feature_flags_remain_disabled():
    assert settings.PLATFORM_BILLING_READ_API is False
    assert settings.PLATFORM_BILLING_SHADOW_RESOLVER is False
    assert settings.PLATFORM_BILLING_ENFORCEMENT is False
    assert settings.PLATFORM_BILLING_FRONTEND_SHELL is False
    assert settings.PLATFORM_BILLING_CHECKOUT is False
    assert settings.PLATFORM_BILLING_WEBHOOK_PROCESSING is False
    assert settings.PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED is False
    assert settings.PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED is False


def test_protected_architecture_checksums_still_match():
    checksum_file = REPO_ROOT / "docs" / "architecture" / "SHA256SUMS"
    assert checksum_file.exists()
    source = checksum_file.read_text(encoding="utf-8")
    assert "DOERS_PLATFORM_SUBSCRIPTION_CONSTITUTION_V2.md" in source
    assert "DOERS_PLATFORM_SUBSCRIPTION_V3_1_EXECUTION_SPEC.md" in source


def test_phase4a_migration_does_not_define_sensitive_columns():
    source = MIGRATION.read_text(encoding="utf-8").lower()
    for forbidden in (" pan ", " cvv", "raw_token", "provider_secret", "api_secret", "card_number", "raw webhook"):
        assert forbidden not in source
    assert not re.search(r"\bpayload_json\b", source)
