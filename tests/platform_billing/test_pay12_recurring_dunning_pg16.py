from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from tests.platform_billing.test_pay11_production_model import (
    PROVIDER_RELEASE_1,
    _seed_pay11_contract,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1, ORG_2, SHA_A, SHA_B, exec_sql, expect_db_error,
)

CUSTOMER = "92000000-0000-0000-0000-000000000001"
MANDATE_1 = "92000000-0000-0000-0000-000000000101"
MANDATE_2 = "92000000-0000-0000-0000-000000000102"
INVOICE = "92000000-0000-0000-0000-000000000201"
JOB = "92000000-0000-0000-0000-000000000301"
CASE = "92000000-0000-0000-0000-000000000401"
NOTICE = "92000000-0000-0000-0000-000000000601"


@pytest.mark.asyncio
async def test_pay12_pg16_durable_lifecycle_invariants():
    ids = await _seed_pay11_contract()
    p = ids | {
        "org1": ORG_1, "customer": CUSTOMER, "provider_release": PROVIDER_RELEASE_1,
        "m1": MANDATE_1, "m2": MANDATE_2, "invoice": INVOICE,
        "job": JOB, "case": CASE, "notice": NOTICE, "sha_a": SHA_A, "sha_b": SHA_B,
    }
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_provider_customers
            (id,organization_id,provider_code,external_customer_ref,status)
        VALUES (:customer,:org1,'fake','pay12_customer','active');

        INSERT INTO platform_mandates
            (id,organization_id,provider_customer_id,provider_release_id,
             provider_code,external_mandate_ref,mandate_type,status,payment_rail,
             currency_code,max_amount_minor,valid_from,valid_until,provider_evidence_sha256)
        VALUES
            (:m1,:org1,:customer,:provider_release,'fake','pay12_m1','recurring',
             'pending','upi_autopay','INR',500000,'2026-09-01T00:00:00Z',
             '2027-09-01T00:00:00Z',:sha_a),
            (:m2,:org1,:customer,:provider_release,'fake','pay12_m2','recurring',
             'pending','card_recurring','INR',500000,'2026-09-01T00:00:00Z',
             '2027-09-01T00:00:00Z',:sha_b);

        UPDATE platform_mandates
        SET status='authorized',authorized_at='2026-09-01T00:01:00Z' WHERE id=:m1;
        UPDATE platform_mandates
        SET status='active',activated_at='2026-09-01T00:02:00Z' WHERE id=:m1;
        UPDATE platform_mandates
        SET status='authorized',authorized_at='2026-09-02T00:01:00Z' WHERE id=:m2;
        UPDATE platform_mandates
        SET status='active',activated_at='2026-09-02T00:02:00Z' WHERE id=:m2;
        UPDATE platform_mandates
        SET replacement_mandate_id=:m2,replaced_at='2026-09-02T00:03:00Z',
            status='revoked',revoked_at='2026-09-02T00:04:00Z'
        WHERE id=:m1;

        INSERT INTO platform_invoices
            (id,organization_id,billing_account_id,subscription_id,status,currency_code,
             subtotal_minor,tax_minor,total_minor,amount_due_minor,catalog_release_id,
             provider_release_id,plan_version_id,price_id,commercial_contract_sha256,
             service_period_start,service_period_end,tax_snapshot_json,billing_address_snapshot_json)
        SELECT :invoice,:org1,billing_account_id,id,'draft','INR',
               10000,1800,11800,11800,accepted_catalog_release_id,
               accepted_provider_release_id,accepted_plan_version_id,accepted_price_id,
               commercial_contract_sha256,'2026-10-01T00:00:00Z','2026-11-01T00:00:00Z',
               '{}'::jsonb,'{}'::jsonb
        FROM platform_subscriptions WHERE id=:subscription;

        INSERT INTO platform_recurring_billing_jobs
            (id,organization_id,subscription_id,period_start,period_end,run_at,
             status,max_attempts,invoice_id)
        VALUES (:job,:org1,:subscription,'2026-10-01T00:00:00Z',
                '2026-11-01T00:00:00Z','2026-10-01T00:00:00Z','scheduled',4,:invoice);

        INSERT INTO platform_dunning_cases
            (id,organization_id,subscription_id,invoice_id,policy_code,policy_snapshot_json,
             status,stage,first_confirmed_failure_at,full_grace_ends_at,
             limited_write_ends_at,read_only_ends_at,confirmed_attempt_count,max_attempts,
             next_retry_at,last_evidence_kind,last_evidence_sha256,last_evidence_ref,last_evidence_at)
        VALUES (:case,:org1,:subscription,:invoice,'DUNNING-IN-V1','{}'::jsonb,
                'open','full_grace','2026-10-01T00:05:00Z','2026-10-04T00:05:00Z',
                '2026-10-08T00:05:00Z','2026-10-15T00:05:00Z',1,4,
                '2026-10-02T00:05:00Z','confirmed_payment_failure',:sha_a,
                'evidence-pay12-failure-1','2026-10-01T00:05:00Z');

        INSERT INTO platform_notification_deliveries
            (id,organization_id,dunning_case_id,notification_type,policy_code,
             channel,recipient_hash,status,scheduled_at,dedupe_key)
        VALUES (:notice,:org1,:case,'payment_failed','DUNNING-IN-V1',
                'email',:sha_b,'queued','2026-10-01T00:06:00Z','pay12-notice-1');
        """,
        p,
    )

    await expect_db_error(
        "SELECT pg_catalog.set_config('app.current_org_id', :org1, true); "
        "UPDATE platform_mandates SET status='active' WHERE id=:m1",
        {"org1": ORG_1, "m1": MANDATE_1},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_dunning_cases
            (organization_id,subscription_id,invoice_id,policy_code,policy_snapshot_json,
             status,stage,first_confirmed_failure_at,full_grace_ends_at,
             limited_write_ends_at,read_only_ends_at,confirmed_attempt_count,max_attempts,
             last_evidence_kind,last_evidence_sha256,last_evidence_ref,last_evidence_at)
        VALUES (:org1,:subscription,:invoice,'DUNNING-IN-V1','{}'::jsonb,'open','full_grace',
                '2026-10-01T00:05:00Z','2026-10-04T00:05:00Z','2026-10-08T00:05:00Z',
                '2026-10-15T00:05:00Z',1,4,'provider_outage',:sha_b,
                'outage-evidence','2026-10-01T00:05:00Z')
        """,
        p,
    )
    await exec_sql(
        "SELECT pg_catalog.set_config('app.current_org_id', :org1, true); "
        "UPDATE platform_dunning_cases SET stage='limited_write',version=version+1 WHERE id=:case",
        {"org1": ORG_1, "case": CASE},
    )
    await expect_db_error(
        "SELECT pg_catalog.set_config('app.current_org_id', :org1, true); "
        "UPDATE platform_dunning_cases SET stage='full_grace' WHERE id=:case",
        {"org1": ORG_1, "case": CASE},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_notification_deliveries
            (organization_id,dunning_case_id,notification_type,policy_code,
             channel,recipient_hash,status,scheduled_at,dedupe_key)
        VALUES (:org1,:case,'payment_failed','DUNNING-IN-V1','email',:sha_b,
                'queued','2026-10-01T00:07:00Z','pay12-notice-1')
        """,
        p,
    )
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_dunning_cases
        SET status='recovered',stage='recovered',recovered_at='2026-10-10T00:00:00Z',
            last_evidence_kind='payment_succeeded',last_evidence_sha256=:sha_b,
            last_evidence_ref='recovery-evidence',last_evidence_at='2026-10-10T00:00:00Z',
            next_retry_at=NULL,version=version+1
        WHERE id=:case
        """,
        p,
    )

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"), {"org1": ORG_1})
        row=(await session.execute(text("SELECT status,stage FROM platform_dunning_cases WHERE id=:id"),{"id":CASE})).one()
        assert tuple(row)==("recovered","recovered")
        await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :org2, true)"), {"org2": ORG_2})
        assert (await session.execute(text("SELECT count(*) FROM platform_dunning_cases WHERE id=:id"),{"id":CASE})).scalar_one()==0
        assert (await session.execute(text("SELECT count(*) FROM platform_recurring_billing_jobs WHERE id=:id"),{"id":JOB})).scalar_one()==0
