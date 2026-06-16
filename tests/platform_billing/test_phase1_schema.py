from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from conftest import cleanup_test_database_tables


ORG_1 = "81000000-0000-0000-0000-000000000001"
ORG_2 = "81000000-0000-0000-0000-000000000002"
OWNER_1 = "82000000-0000-0000-0000-000000000001"
SHA_A = "a" * 64
SHA_B = "b" * 64

PHASE_1_TABLES = [
    "platform_billing_audit_events",
    "platform_subscription_events",
    "platform_subscription_periods",
    "platform_subscription_items",
    "platform_subscriptions",
    "platform_billing_accounts",
    "platform_plan_entitlements",
    "platform_feature_definitions",
    "platform_prices",
    "platform_plan_versions",
    "platform_policy_versions",
    "platform_products",
    "organizations",
]


async def cleanup_phase1_tables() -> None:
    await cleanup_test_database_tables(PHASE_1_TABLES)


async def exec_sql(sql: str, params: dict[str, object] | None = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        for statement in [part.strip() for part in sql.split(";") if part.strip()]:
            await session.execute(text(statement), params or {})
        await session.commit()


async def scalar(sql: str, params: dict[str, object] | None = None) -> object:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar_one()


async def expect_db_error(sql: str, params: dict[str, object] | None = None) -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(Exception):
            for statement in [part.strip() for part in sql.split(";") if part.strip()]:
                await session.execute(text(statement), params or {})
            await session.commit()
        await session.rollback()


async def seed_organizations() -> None:
    await exec_sql(
        """
        INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
        VALUES
            (:org1, 'Platform Billing Org 1', 'platform-billing-org-1', 'basic'::orgtier, true, 5, 'INR'),
            (:org2, 'Platform Billing Org 2', 'platform-billing-org-2', 'basic'::orgtier, true, 5, 'INR')
        """,
        {"org1": ORG_1, "org2": ORG_2},
    )


async def seed_catalog() -> dict[str, str]:
    ids = {
        "product": "83000000-0000-0000-0000-000000000001",
        "trial_policy": "83000000-0000-0000-0000-000000000101",
        "dunning_policy": "83000000-0000-0000-0000-000000000102",
        "cancel_policy": "83000000-0000-0000-0000-000000000103",
        "downgrade_policy": "83000000-0000-0000-0000-000000000104",
        "plan": "83000000-0000-0000-0000-000000000201",
        "draft_plan": "83000000-0000-0000-0000-000000000202",
        "price": "83000000-0000-0000-0000-000000000301",
        "feature": "83000000-0000-0000-0000-000000000401",
        "entitlement": "83000000-0000-0000-0000-000000000501",
    }
    await exec_sql(
        """
        INSERT INTO platform_products (id, code, name, status)
        VALUES (:product, 'DOERS_CORE', 'Doers Core', 'active');

        INSERT INTO platform_policy_versions
            (id, code, policy_type, version, payload, status, payload_sha256, published_at)
        VALUES
            (:trial_policy, 'TRIAL-IN-V1', 'trial', 1, '{"days": 14}'::jsonb, 'published', :sha_a, clock_timestamp()),
            (:dunning_policy, 'DUNNING-IN-V1', 'dunning', 1, '{"days": 14}'::jsonb, 'published', :sha_a, clock_timestamp()),
            (:cancel_policy, 'CANCEL-IN-V1', 'cancellation', 1, '{"days": 30}'::jsonb, 'published', :sha_a, clock_timestamp()),
            (:downgrade_policy, 'DOWNGRADE-IN-V1', 'downgrade', 1, '{"preview_required": true}'::jsonb, 'published', :sha_a, clock_timestamp());

        INSERT INTO platform_plan_versions (
            id, product_id, version, code, display_name, status,
            trial_policy_version_id, dunning_policy_version_id,
            cancellation_policy_version_id, downgrade_policy_version_id,
            metadata_json, published_at
        )
        VALUES (
            :plan, :product, 1, 'DOERS_STARTER_V1', 'Doers Starter', 'published',
            :trial_policy, :dunning_policy, :cancel_policy, :downgrade_policy,
            '{"display": "safe"}'::jsonb, clock_timestamp()
        );

        INSERT INTO platform_plan_versions (
            id, product_id, version, code, display_name, status,
            trial_policy_version_id, dunning_policy_version_id,
            cancellation_policy_version_id, downgrade_policy_version_id,
            metadata_json, published_at
        )
        VALUES (
            :draft_plan, :product, 2, 'DOERS_DRAFT_V2', 'Doers Draft', 'draft',
            :trial_policy, :dunning_policy, :cancel_policy, :downgrade_policy,
            '{}'::jsonb, NULL
        );

        INSERT INTO platform_prices (
            id, plan_version_id, code, currency_code, country_code, billing_interval,
            interval_count, amount_minor, tax_behavior, status, valid_from, published_at
        )
        VALUES (
            :price, :plan, 'DOERS_STARTER_INR_MONTH_V1', 'INR', 'IN', 'month',
            1, 99900, 'exclusive', 'active', '2026-01-01T00:00:00Z', clock_timestamp()
        );

        INSERT INTO platform_feature_definitions
            (id, key, display_name, value_type, enforcement_mode, unit, description, status)
        VALUES
            (:feature, 'limits.branches.active', 'Active branches', 'integer', 'hard', 'branches', 'Maximum active branches', 'active');

        INSERT INTO platform_plan_entitlements
            (id, plan_version_id, feature_definition_id, value_type, value_integer)
        VALUES
            (:entitlement, :plan, :feature, 'integer', 3)
        """,
        ids | {"sha_a": SHA_A},
    )
    return ids


async def seed_billing_account_and_subscription() -> dict[str, str]:
    catalog = await seed_catalog()
    ids = catalog | {
        "billing_account": "84000000-0000-0000-0000-000000000001",
        "subscription": "84000000-0000-0000-0000-000000000101",
        "item": "84000000-0000-0000-0000-000000000201",
        "period": "84000000-0000-0000-0000-000000000301",
    }
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_billing_accounts (
            id, organization_id, status, legal_name, billing_email, country_code,
            default_currency_code, city, created_by, updated_by
        )
        VALUES (
            :billing_account, :org1, 'active', 'Platform Billing Org 1',
            'billing@example.test', 'IN', 'INR', 'Bengaluru', :owner1, :owner1
        );

        INSERT INTO platform_subscriptions (
            id, organization_id, billing_account_id, status, current_plan_version_id,
            current_price_id, policy_snapshot_json, started_at, current_period_start,
            current_period_end, created_by, updated_by
        )
        VALUES (
            :subscription, :org1, :billing_account, 'trialing', :plan,
            NULL, '{"trial_policy": "TRIAL-IN-V1"}'::jsonb,
            '2026-06-15T10:00:00Z', '2026-06-15T10:00:00Z',
            '2026-06-29T10:00:00Z', :owner1, :owner1
        );

        INSERT INTO platform_subscription_items (
            id, organization_id, subscription_id, item_type, plan_version_id,
            price_id, quantity, effective_from, status
        )
        VALUES (
            :item, :org1, :subscription, 'base_plan', :plan,
            NULL, 1, '2026-06-15T10:00:00Z', 'active'
        );

        INSERT INTO platform_subscription_periods (
            id, organization_id, subscription_id, period_type, status,
            starts_at, ends_at, metadata_json
        )
        VALUES (
            :period, :org1, :subscription, 'trial', 'open',
            '2026-06-15T10:00:00Z', '2026-06-29T10:00:00Z',
            '{"source": "test"}'::jsonb
        )
        """,
        ids | {"org1": ORG_1, "owner1": OWNER_1},
    )
    return ids


async def test_phase_1_tables_exist_and_later_phase_tables_absent():
    required = {
        "platform_products",
        "platform_policy_versions",
        "platform_plan_versions",
        "platform_prices",
        "platform_feature_definitions",
        "platform_plan_entitlements",
        "platform_billing_accounts",
        "platform_subscriptions",
        "platform_subscription_items",
        "platform_subscription_periods",
        "platform_subscription_events",
        "platform_billing_audit_events",
    }
    result = await scalar(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(:tables)
        """,
        {"tables": list(required)},
    )
    assert result == len(required)

    forbidden = [
        "platform_provider_customers",
        "platform_webhook_inbox",
        "platform_invoices",
        "platform_payment_attempts",
        "platform_refunds",
        "platform_access_projection",
        "platform_subscription_changes",
    ]
    absent = await scalar(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(:tables)
        """,
        {"tables": forbidden},
    )
    assert absent == 0


async def test_required_constraints_indexes_and_rls_are_present():
    constraints = {
        "uq_platform_billing_accounts_id_org",
        "chk_platform_prices_amount_nonnegative",
        "chk_platform_prices_currency_format",
        "chk_platform_subscription_periods_order",
        "ex_platform_subscription_periods_no_overlap",
        "uq_platform_subscription_events_subscription_sequence",
    }
    found_constraints = await scalar(
        """
        SELECT count(*)
        FROM pg_constraint
        WHERE conname = ANY(:names)
        """,
        {"names": list(constraints)},
    )
    assert found_constraints == len(constraints)

    indexes = {
        "ux_platform_billing_accounts_one_active_per_org",
        "ux_platform_subscriptions_one_current_per_org",
        "ux_platform_subscription_items_one_active_base_plan",
        "ux_platform_subscription_events_source_identity",
        "ux_platform_subscription_events_evidence_identity",
    }
    found_indexes = await scalar(
        """
        SELECT count(*)
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = ANY(:names)
        """,
        {"names": list(indexes)},
    )
    assert found_indexes == len(indexes)

    rls_count = await scalar(
        """
        SELECT count(*)
        FROM pg_class
        WHERE relname = ANY(:tables)
          AND relrowsecurity
          AND relforcerowsecurity
        """,
        {"tables": [
            "platform_billing_accounts",
            "platform_subscriptions",
            "platform_subscription_items",
            "platform_subscription_periods",
            "platform_subscription_events",
            "platform_billing_audit_events",
        ]},
    )
    assert rls_count == 6


async def test_catalog_constraints_and_immutability():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_catalog()

    await expect_db_error(
        """
        INSERT INTO platform_prices (
            plan_version_id, code, currency_code, billing_interval, interval_count,
            amount_minor, tax_behavior, status, valid_from, published_at
        )
        VALUES (:plan, 'BAD_CURRENCY', 'inr', 'month', 1, 1, 'exclusive', 'active', clock_timestamp(), clock_timestamp())
        """,
        ids,
    )

    await expect_db_error(
        "UPDATE platform_policy_versions SET payload = '{\"days\": 15}'::jsonb WHERE id = :trial_policy",
        ids,
    )
    await expect_db_error(
        "UPDATE platform_plan_versions SET display_name = 'Changed' WHERE id = :plan",
        ids,
    )
    await expect_db_error(
        "UPDATE platform_prices SET amount_minor = 1 WHERE id = :price",
        ids,
    )
    await expect_db_error(
        "UPDATE platform_plan_entitlements SET value_integer = 4 WHERE id = :entitlement",
        ids,
    )


async def test_subscription_database_invariants():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_billing_accounts (
            organization_id, status, legal_name, billing_email, country_code, default_currency_code, city
        )
        VALUES (:org1, 'active', 'Duplicate', 'duplicate@example.test', 'IN', 'INR', 'Bengaluru')
        """,
        ids | {"org1": ORG_1},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscriptions (
            organization_id, billing_account_id, status, current_plan_version_id,
            policy_snapshot_json, started_at, current_period_start, current_period_end
        )
        VALUES (
            :org1, :billing_account, 'active', :plan, '{}'::jsonb,
            '2026-06-16T10:00:00Z', '2026-06-16T10:00:00Z', '2026-07-16T10:00:00Z'
        )
        """,
        ids | {"org1": ORG_1},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_items (
            organization_id, subscription_id, item_type, plan_version_id, quantity, effective_from, status
        )
        VALUES (:org1, :subscription, 'base_plan', :plan, 1, '2026-06-16T10:00:00Z', 'active')
        """,
        ids | {"org1": ORG_1},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_periods (
            organization_id, subscription_id, period_type, status, starts_at, ends_at
        )
        VALUES (
            :org1, :subscription, 'trial', 'open',
            '2026-06-20T10:00:00Z', '2026-06-30T10:00:00Z'
        )
        """,
        ids | {"org1": ORG_1},
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_periods (
            organization_id, subscription_id, period_type, status, starts_at, ends_at
        )
        VALUES (
            :org1, :subscription, 'trial', 'void',
            '2026-06-20T10:00:00Z', '2026-06-30T10:00:00Z'
        )
        """,
        ids | {"org1": ORG_1},
    )


async def test_subscription_events_and_audit_are_append_only_and_deduplicated():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_events (
            organization_id, subscription_id, sequence_number, event_type,
            occurred_at, actor_type, source_type, source_id, evidence_sha256,
            payload_json, payload_sha256
        )
        VALUES (
            :org1, :subscription, 1, 'platform.subscription.created',
            '2026-06-15T10:00:00Z', 'system', 'migration',
            '85000000-0000-0000-0000-000000000001', :sha_a,
            '{"safe": true}'::jsonb, :sha_b
        );

        INSERT INTO platform_billing_audit_events (
            organization_id, actor_type, action, target_type, target_id,
            metadata_redacted_json, outcome
        )
        VALUES (
            :org1, 'system', 'platform.subscription.created', 'platform_subscription',
            :subscription, '{"safe": true}'::jsonb, 'succeeded'
        )
        """,
        ids | {"org1": ORG_1, "sha_a": SHA_A, "sha_b": SHA_B},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_events (
            organization_id, subscription_id, sequence_number, event_type,
            occurred_at, actor_type, source_type, payload_json, payload_sha256
        )
        VALUES (
            :org1, :subscription, 1, 'platform.subscription.other',
            '2026-06-15T10:00:00Z', 'system', 'migration',
            '{}'::jsonb, :sha_b
        )
        """,
        ids | {"org1": ORG_1, "sha_b": SHA_B},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_subscription_events (
            organization_id, subscription_id, sequence_number, event_type,
            occurred_at, actor_type, source_type, source_id, payload_json, payload_sha256
        )
        VALUES (
            :org1, :subscription, 2, 'platform.subscription.created',
            '2026-06-15T10:00:00Z', 'system', 'migration',
            '85000000-0000-0000-0000-000000000001', '{}'::jsonb, :sha_b
        )
        """,
        ids | {"org1": ORG_1, "sha_b": SHA_B},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_subscription_events SET payload_json = '{"mutated": true}'::jsonb
        WHERE subscription_id = :subscription
        """,
        ids | {"org1": ORG_1},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        DELETE FROM platform_billing_audit_events WHERE target_id = :subscription
        """,
        ids | {"org1": ORG_1},
    )


async def test_tenant_rls_and_composite_foreign_keys():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org2, true);
        INSERT INTO platform_subscription_items (
            organization_id, subscription_id, item_type, plan_version_id, quantity, effective_from, status
        )
        VALUES (:org2, :subscription, 'addon', :plan, 1, '2026-06-16T10:00:00Z', 'active')
        """,
        ids | {"org2": ORG_2},
    )

    async with AsyncSessionLocal() as session:
        role_exists = (
            await session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime')")
            )
        ).scalar_one()
        if not role_exists:
            pytest.skip("app_runtime role is not present in this database")

        await session.execute(text("SET ROLE app_runtime"))
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})
        own_count = (
            await session.execute(text("SELECT count(*) FROM platform_billing_accounts"))
        ).scalar_one()
        assert own_count == 1

        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org2, true)"), {"org2": ORG_2})
        other_count = (
            await session.execute(text("SELECT count(*) FROM platform_billing_accounts"))
        ).scalar_one()
        assert other_count == 0

        await session.execute(text("RESET ROLE"))
