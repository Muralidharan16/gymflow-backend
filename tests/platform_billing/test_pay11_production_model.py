from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from tests.platform_billing.platform_billing_test_isolation import (
    create_platform_billing_admin_sessionmaker,
    get_platform_billing_test_config,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    ORG_2,
    SHA_A,
    SHA_B,
    cleanup_phase1_tables,
    exec_sql,
    expect_db_error,
    seed_billing_account_and_subscription,
    seed_organizations,
)


CATALOG_RELEASE_1 = "91000000-0000-0000-0000-000000000001"
CATALOG_RELEASE_2 = "91000000-0000-0000-0000-000000000002"
PROVIDER_RELEASE_1 = "91000000-0000-0000-0000-000000000101"
PROVIDER_CUSTOMER = "91000000-0000-0000-0000-000000000201"
PROVIDER_SUBSCRIPTION = "91000000-0000-0000-0000-000000000301"
MANDATE = "91000000-0000-0000-0000-000000000401"
INVOICE_SEQUENCE = "91000000-0000-0000-0000-000000000501"
CREDIT_SEQUENCE = "91000000-0000-0000-0000-000000000502"
INVOICE = "91000000-0000-0000-0000-000000000601"
INVOICE_LINE = "91000000-0000-0000-0000-000000000602"
PAYMENT = "91000000-0000-0000-0000-000000000701"
CREDIT_NOTE = "91000000-0000-0000-0000-000000000801"
CREDIT_NOTE_LINE = "91000000-0000-0000-0000-000000000802"
REFUND = "91000000-0000-0000-0000-000000000901"


async def _seed_pay11_contract() -> dict[str, str]:
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()

    await exec_sql(
        """
        INSERT INTO platform_catalog_releases (
            id, code, version, status, manifest_json
        ) VALUES (
            :catalog_release_1, 'catalog_release_v1', 1, 'draft',
            '{"source":"pay11-test"}'::jsonb
        );

        INSERT INTO platform_catalog_release_items (
            catalog_release_id, plan_version_id, price_id, item_sha256
        ) VALUES (
            :catalog_release_1, :plan, :price, :sha_a
        );

        UPDATE platform_catalog_releases
        SET status='published', manifest_sha256=:sha_b, published_at=clock_timestamp()
        WHERE id=:catalog_release_1;

        INSERT INTO platform_provider_releases (
            id, code, version, provider_code, environment, adapter_version,
            contract_sha256, status, manifest_json
        ) VALUES (
            :provider_release_1, 'provider_release_v1', 1, 'fake', 'test', 'fake-v1',
            :sha_a, 'draft', '{"source":"pay11-test"}'::jsonb
        );

        UPDATE platform_provider_releases
        SET status='published', manifest_sha256=:sha_b, published_at=clock_timestamp()
        WHERE id=:provider_release_1;

        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_subscriptions
        SET current_price_id=:price,
            accepted_catalog_release_id=:catalog_release_1,
            accepted_provider_release_id=:provider_release_1,
            accepted_plan_version_id=:plan,
            accepted_price_id=:price,
            commercial_contract_sha256=:sha_a,
            commercial_contract_accepted_at=clock_timestamp()
        WHERE id=:subscription;
        """,
        ids
        | {
            "org1": ORG_1,
            "catalog_release_1": CATALOG_RELEASE_1,
            "provider_release_1": PROVIDER_RELEASE_1,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
        },
    )
    return ids


@pytest.mark.asyncio
async def test_pay11_release_immutability_contract_retention_and_controlled_migration():
    ids = await _seed_pay11_contract()

    await expect_db_error(
        """
        UPDATE platform_catalog_releases
        SET manifest_json='{"mutated":true}'::jsonb
        WHERE id=:catalog_release_1
        """,
        {"catalog_release_1": CATALOG_RELEASE_1},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_subscriptions
        SET commercial_contract_sha256=:sha_b
        WHERE id=:subscription
        """,
        ids | {"org1": ORG_1, "sha_b": SHA_B},
    )

    await exec_sql(
        """
        INSERT INTO platform_catalog_releases (
            id, code, version, status, manifest_json
        ) VALUES (
            :catalog_release_2, 'catalog_release_v2', 2, 'draft',
            '{"source":"pay11-controlled-migration"}'::jsonb
        );
        INSERT INTO platform_catalog_release_items (
            catalog_release_id, plan_version_id, price_id, item_sha256
        ) VALUES (
            :catalog_release_2, :plan, :price, :sha_b
        );
        UPDATE platform_catalog_releases
        SET status='published', manifest_sha256=:sha_a, published_at=clock_timestamp()
        WHERE id=:catalog_release_2;
        """,
        ids
        | {
            "catalog_release_2": CATALOG_RELEASE_2,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
        },
    )

    config = get_platform_billing_test_config()
    engine, admin_sessions = create_platform_billing_admin_sessionmaker(config)
    try:
        async with admin_sessions() as session:
            await session.execute(
                text(
                    """
                    SELECT public.migrate_platform_subscription_commercial_contract(
                        CAST(:subscription AS uuid),
                        CAST(:org1 AS uuid),
                        CAST(:catalog_release_2 AS uuid),
                        CAST(:provider_release_1 AS uuid),
                        CAST(:plan AS uuid),
                        CAST(:price AS uuid),
                        CAST(:sha_b AS char(64)),
                        'PAY-11 controlled migration test',
                        NULL
                    )
                    """
                ),
                ids
                | {
                    "org1": ORG_1,
                    "catalog_release_2": CATALOG_RELEASE_2,
                    "provider_release_1": PROVIDER_RELEASE_1,
                    "sha_b": SHA_B,
                },
            )
            await session.commit()
    finally:
        await engine.dispose()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT accepted_catalog_release_id, commercial_contract_sha256,
                           commercial_contract_migrated_at, commercial_contract_migration_reason
                    FROM platform_subscriptions
                    WHERE id=:subscription
                    """
                ),
                {"subscription": ids["subscription"]},
            )
        ).one()
        assert str(row[0]) == CATALOG_RELEASE_2
        assert row[1].strip() == SHA_B
        assert row[2] is not None
        assert row[3] == "PAY-11 controlled migration test"

        audit_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM platform_billing_audit_events
                    WHERE target_id=:subscription
                      AND action='platform.subscription.commercial_contract_migrated'
                    """
                ),
                {"subscription": ids["subscription"]},
            )
        ).scalar_one()
        assert audit_count == 1


@pytest.mark.asyncio
async def test_pay11_provider_invoice_payment_credit_and_refund_invariants():
    ids = await _seed_pay11_contract()

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_provider_customers (
            id, organization_id, provider_code, external_customer_ref, status
        ) VALUES (
            :provider_customer, :org1, 'fake', 'fake_customer_pay11', 'active'
        );

        INSERT INTO platform_provider_subscriptions (
            id, organization_id, subscription_id, provider_customer_id,
            provider_release_id, provider_code, external_subscription_ref,
            status, current_period_start, current_period_end,
            provider_evidence_sha256
        ) VALUES (
            :provider_subscription, :org1, :subscription, :provider_customer,
            :provider_release_1, 'fake', 'fake_subscription_pay11',
            'active', '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z', :sha_a
        );

        INSERT INTO platform_mandates (
            id, organization_id, provider_customer_id, provider_subscription_id,
            provider_release_id, provider_code, external_mandate_ref,
            mandate_type, status, currency_code, max_amount_minor,
            valid_from, provider_evidence_sha256
        ) VALUES (
            :mandate, :org1, :provider_customer, :provider_subscription,
            :provider_release_1, 'fake', 'fake_mandate_pay11',
            'recurring', 'pending', 'INR', 500000,
            '2026-09-01T00:00:00Z', :sha_a
        );

        UPDATE platform_mandates
        SET status='authorized', authorized_at='2026-09-01T00:00:01Z'
        WHERE id=:mandate;

        UPDATE platform_mandates
        SET status='active', activated_at='2026-09-01T00:00:02Z'
        WHERE id=:mandate;

        INSERT INTO platform_document_sequences (
            id, legal_entity_code, document_type, financial_year,
            series_code, prefix, padding, last_number, status
        ) VALUES
            (:invoice_sequence, 'DOERS_IN', 'invoice', '2026-27', 'DEFAULT', 'INV', 6, 1, 'active'),
            (:credit_sequence, 'DOERS_IN', 'credit_note', '2026-27', 'DEFAULT', 'CN', 6, 1, 'active');

        INSERT INTO platform_invoices (
            id, organization_id, billing_account_id, subscription_id,
            status, currency_code, subtotal_minor, tax_minor, total_minor,
            amount_due_minor, catalog_release_id, provider_release_id,
            plan_version_id, price_id, commercial_contract_sha256,
            service_period_start, service_period_end,
            tax_snapshot_json, billing_address_snapshot_json
        ) VALUES (
            :invoice, :org1, :billing_account, :subscription,
            'draft', 'INR', 10000, 1800, 11800,
            11800, :catalog_release_1, :provider_release_1,
            :plan, :price, :sha_a,
            '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z',
            '{"gst":"18pct"}'::jsonb, '{"country":"IN"}'::jsonb
        );

        INSERT INTO platform_invoice_lines (
            id, organization_id, invoice_id, line_number, line_type,
            description, quantity, unit_amount_minor, net_amount_minor,
            tax_rate_bps, tax_amount_minor, gross_amount_minor,
            plan_version_id, price_id
        ) VALUES (
            :invoice_line, :org1, :invoice, 1, 'subscription',
            'Doers subscription', 1, 10000, 10000,
            1800, 1800, 11800, :plan, :price
        );

        UPDATE platform_invoices
        SET status='issued',
            document_sequence_id=:invoice_sequence,
            invoice_number='INV000001',
            issued_at=clock_timestamp()
        WHERE id=:invoice;

        INSERT INTO platform_payment_attempts (
            id, organization_id, invoice_id, mandate_id, provider_release_id,
            provider_code, external_payment_ref, idempotency_key, attempt_number,
            amount_minor, currency_code, status, provider_evidence_sha256,
            provider_evidence_ref, completed_at
        ) VALUES (
            :payment, :org1, :invoice, :mandate, :provider_release_1,
            'fake', 'fake_payment_pay11', 'pay11-payment-1', 1,
            11800, 'INR', 'succeeded', :sha_b,
            'evidence://pay11/payment/1', clock_timestamp()
        );

        INSERT INTO platform_credit_notes (
            id, organization_id, invoice_id, status, reason_code,
            currency_code, subtotal_minor, tax_minor, total_minor
        ) VALUES (
            :credit_note, :org1, :invoice, 'draft', 'partial_refund',
            'INR', 5000, 900, 5900
        );

        INSERT INTO platform_credit_note_lines (
            id, organization_id, credit_note_id, invoice_line_id,
            line_number, line_type, description,
            net_amount_minor, tax_amount_minor, gross_amount_minor
        ) VALUES (
            :credit_note_line, :org1, :credit_note, :invoice_line,
            1, 'charge_reversal', 'Partial subscription reversal',
            5000, 900, 5900
        );

        UPDATE platform_credit_notes
        SET status='issued',
            document_sequence_id=:credit_sequence,
            credit_note_number='CN000001',
            issued_at=clock_timestamp()
        WHERE id=:credit_note;

        INSERT INTO platform_refunds (
            id, organization_id, payment_attempt_id, invoice_id, credit_note_id,
            provider_release_id, provider_code, provider_refund_ref,
            idempotency_key, amount_minor, currency_code, reason_code,
            status, provider_evidence_sha256, provider_evidence_ref, completed_at
        ) VALUES (
            :refund, :org1, :payment, :invoice, :credit_note,
            :provider_release_1, 'fake', 'fake_refund_pay11',
            'pay11-refund-1', 5900, 'INR', 'customer_refund',
            'succeeded', :sha_b, 'evidence://pay11/refund/1', clock_timestamp()
        );
        """,
        ids
        | {
            "org1": ORG_1,
            "catalog_release_1": CATALOG_RELEASE_1,
            "provider_release_1": PROVIDER_RELEASE_1,
            "provider_customer": PROVIDER_CUSTOMER,
            "provider_subscription": PROVIDER_SUBSCRIPTION,
            "mandate": MANDATE,
            "invoice_sequence": INVOICE_SEQUENCE,
            "credit_sequence": CREDIT_SEQUENCE,
            "invoice": INVOICE,
            "invoice_line": INVOICE_LINE,
            "payment": PAYMENT,
            "credit_note": CREDIT_NOTE,
            "credit_note_line": CREDIT_NOTE_LINE,
            "refund": REFUND,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_invoice_lines
        SET description='Illegal post-issue mutation'
        WHERE id=:invoice_line
        """,
        {"org1": ORG_1, "invoice_line": INVOICE_LINE},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_refunds (
            organization_id, payment_attempt_id, invoice_id,
            provider_release_id, provider_code, idempotency_key,
            amount_minor, currency_code, reason_code, status
        ) VALUES (
            :org1, :payment, :invoice,
            :provider_release_1, 'fake', 'pay11-refund-over-capacity',
            6000, 'INR', 'over_capacity_probe', 'requested'
        )
        """,
        {
            "org1": ORG_1,
            "payment": PAYMENT,
            "invoice": INVOICE,
            "provider_release_1": PROVIDER_RELEASE_1,
        },
    )


    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_payment_attempts
        SET status='failed'
        WHERE id=:payment
        """,
        {"org1": ORG_1, "payment": PAYMENT},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_payment_attempts
        SET amount_minor=11700
        WHERE id=:payment
        """,
        {"org1": ORG_1, "payment": PAYMENT},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_refunds
        SET status='failed'
        WHERE id=:refund
        """,
        {"org1": ORG_1, "refund": REFUND},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_refunds
        SET amount_minor=5800
        WHERE id=:refund
        """,
        {"org1": ORG_1, "refund": REFUND},
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        assert (
            await session.execute(
                text("SELECT count(*) FROM platform_invoices WHERE id=:invoice"),
                {"invoice": INVOICE},
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                text("SELECT count(*) FROM platform_refunds WHERE id=:refund"),
                {"refund": REFUND},
            )
        ).scalar_one() == 1

        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org2, true)"),
            {"org2": ORG_2},
        )
        assert (
            await session.execute(
                text("SELECT count(*) FROM platform_invoices WHERE id=:invoice"),
                {"invoice": INVOICE},
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM platform_refunds WHERE id=:refund"),
                {"refund": REFUND},
            )
        ).scalar_one() == 0
