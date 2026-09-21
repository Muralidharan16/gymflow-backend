from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.platform_billing.repositories.recurring import PlatformRecurringBillingRepository
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
DUNNING_ATTEMPT = "92000000-0000-0000-0000-000000000501"
MANDATE_3 = "92000000-0000-0000-0000-000000000103"
BAD_JOB = "92000000-0000-0000-0000-000000000302"


@pytest.mark.asyncio
async def test_pay12_pg16_durable_lifecycle_invariants():
    ids = await _seed_pay11_contract()
    p = ids | {
        "org1": ORG_1, "customer": CUSTOMER, "provider_release": PROVIDER_RELEASE_1,
        "m1": MANDATE_1, "m2": MANDATE_2, "invoice": INVOICE,
        "job": JOB, "case": CASE, "notice": NOTICE, "dunning_attempt": DUNNING_ATTEMPT,
        "sha_a": SHA_A, "sha_b": SHA_B,
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

        INSERT INTO platform_dunning_attempts
            (id,organization_id,dunning_case_id,payment_attempt_id,
             attempt_number,outcome,counts_toward_dunning,
             evidence_sha256,evidence_ref,observed_at,next_retry_at)
        VALUES (:dunning_attempt,:org1,:case,NULL,1,
                'confirmed_payment_failure',true,:sha_a,
                'evidence-pay12-failure-1','2026-10-01T00:05:00Z',
                '2026-10-02T00:05:00Z');

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
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_mandates
            (id,organization_id,provider_customer_id,provider_release_id,
             provider_code,external_mandate_ref,mandate_type,status,payment_rail,
             currency_code,max_amount_minor,valid_from,activated_at,provider_evidence_sha256)
        VALUES (:m3,:org1,:customer,:provider_release,'fake','pay12_m3','recurring',
                'active','e_mandate','INR',500000,'2026-09-03T00:00:00Z',
                '2026-09-03T00:01:00Z',:sha_a)
        """,
        p | {"m3": MANDATE_3},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_recurring_billing_jobs
            (id,organization_id,subscription_id,period_start,period_end,run_at,
             status,attempt_count,max_attempts,lease_owner,lease_until,lease_fence)
        VALUES (:bad_job,:org1,:subscription,'2026-11-01T00:00:00Z',
                '2026-12-01T00:00:00Z','2026-11-01T00:00:00Z',
                'processing',1,4,'92000000-0000-0000-0000-000000000999',
                '2026-11-01T00:05:00Z',1)
        """,
        p | {"bad_job": BAD_JOB},
    )
    await expect_db_error(
        "SELECT pg_catalog.set_config('app.current_org_id', :org1, true); "
        "UPDATE platform_dunning_attempts SET evidence_ref='tampered' WHERE id=:attempt",
        {"org1": ORG_1, "attempt": DUNNING_ATTEMPT},
    )
    await expect_db_error(
        "SELECT pg_catalog.set_config('app.current_org_id', :org1, true); "
        "DELETE FROM platform_dunning_attempts WHERE id=:attempt",
        {"org1": ORG_1, "attempt": DUNNING_ATTEMPT},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_dunning_cases
        SET policy_snapshot_json='{"tampered":true}'::jsonb
        WHERE id=:case
        """,
        {"org1": ORG_1, "case": CASE},
    )
    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_notification_deliveries
        SET dedupe_key='pay12-notice-tampered'
        WHERE id=:notice
        """,
        {"org1": ORG_1, "notice": NOTICE},
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



@pytest.mark.asyncio
async def test_pay12_recurring_job_final_attempt_crash_reclaim_and_exact_owner():
    ids = await _seed_pay11_contract()
    p = ids | {"org1": ORG_1, "job": JOB}

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_recurring_billing_jobs
            (id,organization_id,subscription_id,period_start,period_end,run_at,
             status,attempt_count,max_attempts)
        VALUES (:job,:org1,:subscription,'2026-10-01T00:00:00Z',
                '2026-11-01T00:00:00Z','2026-10-01T00:00:00Z',
                'scheduled',0,4)
        """,
        p,
    )

    worker_1 = uuid.UUID("92000000-0000-0000-0000-000000000701")
    worker_2 = uuid.UUID("92000000-0000-0000-0000-000000000702")
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    # Reach the fourth/final provider attempt through real claims and retries.
    for expected_attempt in (1, 2, 3):
        claim_time = now + timedelta(minutes=expected_attempt - 1)
        async with AsyncSessionLocal() as session:
            repo = PlatformRecurringBillingRepository(session)
            claim = await repo.claim_due(
                organization_id=uuid.UUID(ORG_1),
                worker_id=worker_1,
                now=claim_time,
                lease_seconds=60,
            )
            assert claim is not None
            assert claim.attempt_number == expected_attempt
            await repo.release_for_retry(
                claim=claim,
                next_attempt_at=claim_time + timedelta(minutes=1),
                error_code="provider_declined",
                evidence_sha256=SHA_A,
                evidence_ref=f"evidence-pay12-attempt-{expected_attempt}",
                now=claim_time + timedelta(seconds=1),
            )
            await session.commit()

    now = now + timedelta(minutes=3)
    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        claim_1 = await repo.claim_due(
            organization_id=uuid.UUID(ORG_1),
            worker_id=worker_1,
            now=now,
            lease_seconds=60,
        )
        assert claim_1 is not None
        assert claim_1.attempt_number == 4
        assert claim_1.max_attempts == 4
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        duplicate = await repo.claim_due(
            organization_id=uuid.UUID(ORG_1),
            worker_id=worker_2,
            now=now + timedelta(seconds=30),
            lease_seconds=60,
        )
        assert duplicate is None
        await session.rollback()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        reclaimed = await repo.claim_due(
            organization_id=uuid.UUID(ORG_1),
            worker_id=worker_2,
            now=now + timedelta(seconds=61),
            lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.attempt_number == 4
        assert reclaimed.lease_fence == claim_1.lease_fence + 1
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        wrong_owner = replace(reclaimed, worker_id=worker_1)
        with pytest.raises(RuntimeError, match="lease lost"):
            await repo.complete(
                claim=wrong_owner,
                invoice_id=uuid.UUID("92000000-0000-0000-0000-000000000201"),
                payment_attempt_id=None,
                evidence_sha256=SHA_A,
                evidence_ref="evidence-pay12-wrong-owner",
                now=now + timedelta(seconds=62),
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        with pytest.raises(ValueError, match="retry budget exhausted"):
            await repo.release_for_retry(
                claim=reclaimed,
                next_attempt_at=now + timedelta(hours=24),
                error_code="provider_declined",
                evidence_sha256=SHA_A,
                evidence_ref="evidence-pay12-final-attempt",
                now=now + timedelta(seconds=62),
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        await repo.release_for_retry(
            claim=reclaimed,
            next_attempt_at=now + timedelta(minutes=10),
            error_code="provider_outcome_unknown",
            evidence_sha256=SHA_B,
            evidence_ref="evidence-pay12-reconcile",
            now=now + timedelta(seconds=62),
            awaiting_reconciliation=True,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = PlatformRecurringBillingRepository(session)
        reconciliation_claim = await repo.claim_due(
            organization_id=uuid.UUID(ORG_1),
            worker_id=worker_2,
            now=now + timedelta(minutes=11),
            lease_seconds=60,
        )
        assert reconciliation_claim is not None
        assert reconciliation_claim.attempt_number == 4
        assert reconciliation_claim.max_attempts == 4
        await session.rollback()


@pytest.mark.asyncio
async def test_pay12_pg16_policy_controlled_suspension_then_termination():
    ids = await _seed_pay11_contract()
    invoice_id = "92000000-0000-0000-0000-000000000211"
    case_id = "92000000-0000-0000-0000-000000000411"
    params = ids | {
        "org1": ORG_1,
        "invoice": invoice_id,
        "case": case_id,
        "sha_a": SHA_A,
    }

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_invoices (
            id, organization_id, billing_account_id, subscription_id,
            status, currency_code, subtotal_minor, tax_minor, total_minor,
            amount_due_minor, catalog_release_id, provider_release_id,
            plan_version_id, price_id, commercial_contract_sha256,
            tax_snapshot_json, billing_address_snapshot_json,
            service_period_start, service_period_end
        )
        SELECT
            :invoice, :org1, billing_account_id, id,
            'draft', 'INR', 10000, 1800, 11800, 11800,
            accepted_catalog_release_id, accepted_provider_release_id,
            accepted_plan_version_id, accepted_price_id,
            commercial_contract_sha256,
            '{}'::jsonb, '{}'::jsonb,
            '2026-12-01T00:00:00Z', '2027-01-01T00:00:00Z'
        FROM platform_subscriptions
        WHERE id=:subscription;

        INSERT INTO platform_dunning_cases (
            id, organization_id, subscription_id, invoice_id,
            policy_code, policy_snapshot_json,
            status, stage, first_confirmed_failure_at,
            full_grace_ends_at, limited_write_ends_at, read_only_ends_at,
            confirmed_attempt_count, max_attempts, next_retry_at,
            last_evidence_kind, last_evidence_sha256,
            last_evidence_ref, last_evidence_at
        ) VALUES (
            :case, :org1, :subscription, :invoice,
            'DUNNING-IN-V1',
            '{"final_action":"suspend_then_terminate","termination_after_suspension_days":30}'::jsonb,
            'open', 'full_grace', '2026-12-01T00:05:00Z',
            '2026-12-04T00:05:00Z', '2026-12-08T00:05:00Z',
            '2026-12-15T00:05:00Z',
            4, 4, NULL,
            'confirmed_payment_failure', :sha_a,
            'evidence-pay12-terminal-policy',
            '2026-12-01T00:05:00Z'
        );
        """,
        params,
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_dunning_cases
        SET status='terminated',
            stage='billing_only',
            terminated_at='2027-01-14T00:05:00Z'
        WHERE id=:case
        """,
        {"org1": ORG_1, "case": case_id},
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_dunning_cases
        SET status='suspended',
            stage='billing_only',
            suspended_at='2026-12-15T00:05:00Z',
            restricted_at='2026-12-04T00:05:00Z',
            version=version+1
        WHERE id=:case;

        UPDATE platform_dunning_cases
        SET status='terminated',
            terminated_at='2027-01-14T00:05:00Z',
            version=version+1
        WHERE id=:case;
        """,
        {"org1": ORG_1, "case": case_id},
    )
