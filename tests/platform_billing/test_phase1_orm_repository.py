from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.models.base import Base
from app.platform_billing.domain.read_models import ProductRead
from app.platform_billing.models import (
    PlatformBillingAccount,
    PlatformBillingAuditEvent,
    PlatformFeatureDefinition,
    PlatformPlanEntitlement,
    PlatformPlanVersion,
    PlatformPolicyVersion,
    PlatformPrice,
    PlatformProduct,
    PlatformSubscription,
    PlatformSubscriptionEvent,
    PlatformSubscriptionItem,
    PlatformSubscriptionPeriod,
)
from app.platform_billing.repositories import (
    PlatformBillingAccountReadRepository,
    PlatformBillingAuditReadRepository,
    PlatformCatalogReadRepository,
    PlatformSubscriptionReadRepository,
)
from app.platform_billing.services import PlatformBillingQueryService
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    cleanup_phase1_tables,
    seed_billing_account_and_subscription,
    seed_organizations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_orm_models_import_and_match_phase_1_tables():
    models = [
        PlatformProduct,
        PlatformPolicyVersion,
        PlatformPlanVersion,
        PlatformPrice,
        PlatformFeatureDefinition,
        PlatformPlanEntitlement,
        PlatformBillingAccount,
        PlatformSubscription,
        PlatformSubscriptionItem,
        PlatformSubscriptionPeriod,
        PlatformSubscriptionEvent,
        PlatformBillingAuditEvent,
    ]
    table_names = {model.__tablename__ for model in models}
    assert table_names == {
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
    for model in models:
        assert model.__table__ in Base.metadata.tables.values()


def test_json_metadata_columns_use_safe_attribute_names():
    assert "metadata_json" in PlatformPlanVersion.__mapper__.attrs
    assert "metadata_json" in PlatformSubscriptionPeriod.__mapper__.attrs
    assert "payload_json" in PlatformSubscriptionEvent.__mapper__.attrs
    assert "metadata_redacted_json" in PlatformBillingAuditEvent.__mapper__.attrs


def test_platform_billing_models_do_not_import_provider_sdks_or_facility_commerce():
    forbidden_terms = [
        "razorpay",
        "cashfree",
        "stripe",
        "app.models.subscription",
        "app.models.payment",
        "app.services.subscription_service",
        "app.services.payment_service",
    ]
    for py_file in (REPO_ROOT / "app" / "platform_billing").rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in source, f"{py_file.relative_to(REPO_ROOT)} contains forbidden reference {term}"


async def test_read_repositories_return_expected_platform_billing_read_models():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})

        catalog = PlatformCatalogReadRepository(session)
        products = await catalog.list_active_products()
        assert [product.code for product in products] == ["DOERS_CORE"]
        assert isinstance(products[0], ProductRead)

        plans = await catalog.list_published_plan_versions(country_code="IN", currency_code="INR")
        assert [plan.code for plan in plans] == ["DOERS_STARTER_V1"]
        assert plans[0].prices[0].money.amount_minor == 99900

        plan_detail = await catalog.get_published_plan_version(ids["plan"], country_code="IN", currency_code="INR")
        assert plan_detail is not None
        assert plan_detail.entitlements[0].feature_key == "limits.branches.active"
        assert plan_detail.entitlements[0].value_integer == 3

        billing_accounts = PlatformBillingAccountReadRepository(session)
        billing_account = await billing_accounts.get_active_for_organization(ids_as_uuid(ORG_1))
        assert billing_account is not None
        assert billing_account.billing_email == "billing@example.test"

        subscriptions = PlatformSubscriptionReadRepository(session)
        current = await subscriptions.get_current_for_organization(ids_as_uuid(ORG_1))
        assert current is not None
        assert current.status == "trialing"

        items = await subscriptions.list_items(organization_id=ids_as_uuid(ORG_1), subscription_id=ids_as_uuid(ids["subscription"]))
        assert [item.item_type for item in items] == ["base_plan"]

        periods = await subscriptions.list_periods(organization_id=ids_as_uuid(ORG_1), subscription_id=ids_as_uuid(ids["subscription"]))
        assert [period.period_type for period in periods] == ["trial"]


async def test_events_are_ordered_and_audit_is_paginated():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})
        statements = [
            """
            INSERT INTO platform_subscription_events (
                organization_id, subscription_id, sequence_number, event_type,
                occurred_at, actor_type, source_type, payload_json, payload_sha256
            )
            VALUES
                (:org1, :sub, 2, 'platform.subscription.second', clock_timestamp(), 'system', 'migration', '{}'::jsonb, :sha),
                (:org1, :sub, 1, 'platform.subscription.first', clock_timestamp(), 'system', 'migration', '{}'::jsonb, :sha)
            """,
            """
            INSERT INTO platform_billing_audit_events (
                organization_id, actor_type, action, target_type, target_id,
                metadata_redacted_json, outcome
            )
            VALUES
                (:org1, 'system', 'first', 'platform_subscription', :sub, '{}'::jsonb, 'succeeded'),
                (:org1, 'system', 'second', 'platform_subscription', :sub, '{}'::jsonb, 'succeeded')
            """,
        ]
        for statement in statements:
            await session.execute(text(statement), {"org1": ORG_1, "sub": ids["subscription"], "sha": "c" * 64})
        await session.commit()

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})
        subscriptions = PlatformSubscriptionReadRepository(session)
        events = await subscriptions.list_events(
            organization_id=ids_as_uuid(ORG_1),
            subscription_id=ids_as_uuid(ids["subscription"]),
        )
        assert [event.sequence_number for event in events] == [1, 2]

        audit = PlatformBillingAuditReadRepository(session)
        page = await audit.list_for_organization(ids_as_uuid(ORG_1), limit=1)
        assert len(page) == 1


async def test_query_service_composes_read_repositories_without_mutation():
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})
        service = PlatformBillingQueryService(session)

        detail = await service.get_current_subscription(ids_as_uuid(ORG_1))
        assert detail is not None
        assert detail.subscription.id == ids_as_uuid(ids["subscription"])
        assert len(detail.items) == 1
        assert len(detail.periods) == 1

        assert await service.get_billing_account(ids_as_uuid(ORG_1)) is not None
        assert await service.list_published_plans(country_code="IN", currency_code="INR")


def ids_as_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)
