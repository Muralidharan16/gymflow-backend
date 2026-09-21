from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    ORG_2,
    SHA_A,
    SHA_B,
    cleanup_phase1_tables,
    exec_sql,
    expect_db_error,
    seed_organizations,
)


UTC = timezone.utc
CLOSE_ACTOR = "96000000-0000-0000-0000-000000000001"

RUN_MATCH = "96000000-0000-0000-0000-000000000101"
EV_LOCAL = "96000000-0000-0000-0000-000000000201"
EV_PROVIDER = "96000000-0000-0000-0000-000000000202"
EV_SETTLEMENT = "96000000-0000-0000-0000-000000000203"
ITEM_MATCH = "96000000-0000-0000-0000-000000000301"

RUN_INCIDENT = "96000000-0000-0000-0000-000000000111"
EV_I_LOCAL = "96000000-0000-0000-0000-000000000211"
EV_I_PROVIDER = "96000000-0000-0000-0000-000000000212"
EV_I_SETTLEMENT = "96000000-0000-0000-0000-000000000213"
ITEM_INCIDENT = "96000000-0000-0000-0000-000000000311"
INCIDENT = "96000000-0000-0000-0000-000000000401"

RUN_RETRY = "96000000-0000-0000-0000-000000000121"
EV_R_LOCAL = "96000000-0000-0000-0000-000000000221"
EV_R_PROVIDER = "96000000-0000-0000-0000-000000000222"
ITEM_RETRY = "96000000-0000-0000-0000-000000000321"


async def _seed_run(run_id: str, closure_key: str) -> None:
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_accounting_closure_runs (
            id, organization_id, provider_code, environment,
            closure_key, period_start, period_end,
            status, expected_object_count
        ) VALUES (
            :run_id, :org1, 'fake', 'test',
            :closure_key,
            '2026-09-01T00:00:00Z',
            '2026-10-01T00:00:00Z',
            'collecting', 1
        );
        """,
        {
            "org1": ORG_1,
            "run_id": run_id,
            "closure_key": closure_key,
        },
    )


async def _insert_evidence(
    *,
    run_id: str,
    evidence_id: str,
    side: str,
    amount_minor: int,
    evidence_kind: str,
    sha: str,
    object_ref: str = "pay_pay14_1",
) -> None:
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_accounting_evidence (
            id, closure_run_id, organization_id,
            side, object_type, object_ref,
            provider_code, environment,
            amount_minor, fee_minor, currency_code, object_status,
            evidence_kind, evidence_sha256, evidence_ref,
            authoritative, observed_at, safe_metadata_json
        ) VALUES (
            :evidence_id, :run_id, :org1,
            :side, 'captured_payment', :object_ref,
            'fake', 'test',
            :amount_minor, 200, 'INR', 'succeeded',
            :evidence_kind, :sha, :evidence_ref,
            true, '2026-09-30T12:00:00Z',
            jsonb_build_object('pay14', 'safe')
        );
        """,
        {
            "evidence_id": evidence_id,
            "run_id": run_id,
            "org1": ORG_1,
            "side": side,
            "amount_minor": amount_minor,
            "evidence_kind": evidence_kind,
            "sha": sha,
            "evidence_ref": f"evidence://pay14/{evidence_id}",
            "object_ref": object_ref,
        },
    )


@pytest.mark.asyncio
async def test_pay14_authoritative_three_way_match_closes_without_money_mutation():
    await cleanup_phase1_tables()
    await seed_organizations()
    await _seed_run(RUN_MATCH, "pay14-close-2026-09")

    await _insert_evidence(
        run_id=RUN_MATCH,
        evidence_id=EV_LOCAL,
        side="local",
        amount_minor=11800,
        evidence_kind="local_snapshot",
        sha=SHA_A,
    )
    await _insert_evidence(
        run_id=RUN_MATCH,
        evidence_id=EV_PROVIDER,
        side="provider",
        amount_minor=11800,
        evidence_kind="provider_statement",
        sha=SHA_B,
    )
    await _insert_evidence(
        run_id=RUN_MATCH,
        evidence_id=EV_SETTLEMENT,
        side="settlement",
        amount_minor=11800,
        evidence_kind="bank_statement",
        sha="c" * 64,
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_accounting_closure_runs
        SET status='reconciling', version=version+1
        WHERE id=:run_id;

        INSERT INTO platform_accounting_reconciliation_items (
            id, closure_run_id, organization_id,
            object_type, reconciliation_key,
            local_evidence_id, provider_evidence_id,
            settlement_evidence_id,
            mismatch_category, safe_outcome, resolution_status,
            authoritative_evidence_complete,
            automatic_financial_mutation_allowed,
            reason_code,
            resolution_evidence_sha256,
            resolution_evidence_ref,
            first_detected_at, last_checked_at, resolved_at
        ) VALUES (
            :item_id, :run_id, :org1,
            'captured_payment', 'fake|test|captured_payment|pay_pay14_1',
            :ev_local, :ev_provider, :ev_settlement,
            NULL, 'auto_resolved_by_authoritative_evidence', 'resolved',
            true, false,
            'PAY14_THREE_WAY_MATCH',
            :sha_b, 'evidence://pay14/resolution/match',
            '2026-09-30T12:01:00Z',
            '2026-09-30T12:01:00Z',
            '2026-09-30T12:01:00Z'
        );

        UPDATE platform_accounting_closure_runs
        SET observed_object_count=1,
            mismatch_count=0,
            retry_count=0,
            manual_review_count=0,
            incident_count=0,
            resolved_count=1,
            evidence_manifest_sha256=:sha_a,
            evidence_manifest_ref='evidence://pay14/manifest/2026-09',
            ready_at='2026-10-01T00:05:00Z',
            status='ready_to_close',
            version=version+1
        WHERE id=:run_id;

        UPDATE platform_accounting_closure_runs
        SET status='closed',
            closed_at='2026-10-01T00:06:00Z',
            closed_by=:closed_by,
            version=version+1
        WHERE id=:run_id;
        """,
        {
            "org1": ORG_1,
            "run_id": RUN_MATCH,
            "item_id": ITEM_MATCH,
            "ev_local": EV_LOCAL,
            "ev_provider": EV_PROVIDER,
            "ev_settlement": EV_SETTLEMENT,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
            "closed_by": CLOSE_ACTOR,
        },
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT status, mismatch_count, resolved_count
                    FROM platform_accounting_closure_runs
                    WHERE id=:run_id
                    """
                ),
                {"run_id": RUN_MATCH},
            )
        ).one()
        assert tuple(row) == ("closed", 0, 1)

        item = (
            await session.execute(
                text(
                    """
                    SELECT safe_outcome, automatic_financial_mutation_allowed
                    FROM platform_accounting_reconciliation_items
                    WHERE id=:item_id
                    """
                ),
                {"item_id": ITEM_MATCH},
            )
        ).one()
        assert tuple(item) == (
            "auto_resolved_by_authoritative_evidence",
            False,
        )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_evidence
        SET amount_minor=11801
        WHERE id=:ev_local
        """,
        {"org1": ORG_1, "ev_local": EV_LOCAL},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_closure_runs
        SET status='reconciling'
        WHERE id=:run_id
        """,
        {"org1": ORG_1, "run_id": RUN_MATCH},
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org2, true)"),
            {"org2": ORG_2},
        )
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM platform_accounting_closure_runs WHERE id=:run_id"
                ),
                {"run_id": RUN_MATCH},
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
async def test_pay14_accounting_incident_blocks_close_until_explicit_resolution():
    await cleanup_phase1_tables()
    await seed_organizations()
    await _seed_run(RUN_INCIDENT, "pay14-incident-2026-09")

    await _insert_evidence(
        run_id=RUN_INCIDENT,
        evidence_id=EV_I_LOCAL,
        side="local",
        amount_minor=11800,
        evidence_kind="local_snapshot",
        sha=SHA_A,
        object_ref="pay_pay14_incident",
    )
    await _insert_evidence(
        run_id=RUN_INCIDENT,
        evidence_id=EV_I_PROVIDER,
        side="provider",
        amount_minor=11700,
        evidence_kind="provider_statement",
        sha=SHA_B,
        object_ref="pay_pay14_incident",
    )
    await _insert_evidence(
        run_id=RUN_INCIDENT,
        evidence_id=EV_I_SETTLEMENT,
        side="settlement",
        amount_minor=11700,
        evidence_kind="bank_statement",
        sha="c" * 64,
        object_ref="pay_pay14_incident",
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_accounting_closure_runs
        SET status='reconciling', version=version+1
        WHERE id=:run_id;

        INSERT INTO platform_accounting_reconciliation_items (
            id, closure_run_id, organization_id,
            object_type, reconciliation_key,
            local_evidence_id, provider_evidence_id,
            settlement_evidence_id,
            mismatch_category, safe_outcome, resolution_status,
            authoritative_evidence_complete,
            automatic_financial_mutation_allowed,
            reason_code,
            first_detected_at, last_checked_at
        ) VALUES (
            :item_id, :run_id, :org1,
            'captured_payment',
            'fake|test|captured_payment|pay_pay14_incident',
            :ev_local, :ev_provider, :ev_settlement,
            'amount_mismatch', 'accounting_incident', 'incident_open',
            false, false,
            'PAY14_AMOUNT_MISMATCH',
            '2026-09-30T13:00:00Z',
            '2026-09-30T13:00:00Z'
        );

        INSERT INTO platform_accounting_incidents (
            id, closure_run_id, reconciliation_item_id,
            organization_id, incident_type, severity,
            status, incident_code,
            evidence_sha256, evidence_ref, opened_at
        ) VALUES (
            :incident_id, :run_id, :item_id,
            :org1, 'accounting_incident', 'warning',
            'open', 'PAY14_AMOUNT_MISMATCH',
            :sha_b, 'evidence://pay14/incident/amount',
            '2026-09-30T13:00:00Z'
        );
        """,
        {
            "org1": ORG_1,
            "run_id": RUN_INCIDENT,
            "item_id": ITEM_INCIDENT,
            "ev_local": EV_I_LOCAL,
            "ev_provider": EV_I_PROVIDER,
            "ev_settlement": EV_I_SETTLEMENT,
            "incident_id": INCIDENT,
            "sha_b": SHA_B,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_closure_runs
        SET observed_object_count=1,
            mismatch_count=1,
            retry_count=0,
            manual_review_count=0,
            incident_count=1,
            resolved_count=0,
            evidence_manifest_sha256=:sha_a,
            evidence_manifest_ref='evidence://pay14/manifest/incident',
            ready_at='2026-10-01T00:05:00Z',
            status='ready_to_close'
        WHERE id=:run_id
        """,
        {"org1": ORG_1, "run_id": RUN_INCIDENT, "sha_a": SHA_A},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_reconciliation_items
        SET automatic_financial_mutation_allowed=true
        WHERE id=:item_id
        """,
        {"org1": ORG_1, "item_id": ITEM_INCIDENT},
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_accounting_incidents
        SET status='resolved',
            resolved_at='2026-10-01T00:10:00Z',
            resolution_code='verified_provider_fee_netting'
        WHERE id=:incident_id;

        UPDATE platform_accounting_reconciliation_items
        SET resolution_status='resolved',
            resolution_evidence_sha256=:sha_a,
            resolution_evidence_ref='evidence://pay14/resolution/incident',
            resolved_at='2026-10-01T00:10:00Z',
            last_checked_at='2026-10-01T00:10:00Z',
            version=version+1
        WHERE id=:item_id;

        UPDATE platform_accounting_closure_runs
        SET observed_object_count=1,
            mismatch_count=1,
            retry_count=0,
            manual_review_count=0,
            incident_count=1,
            resolved_count=1,
            evidence_manifest_sha256=:sha_a,
            evidence_manifest_ref='evidence://pay14/manifest/incident',
            ready_at='2026-10-01T00:11:00Z',
            status='ready_to_close',
            version=version+1
        WHERE id=:run_id;

        UPDATE platform_accounting_closure_runs
        SET status='closed',
            closed_at='2026-10-01T00:12:00Z',
            closed_by=:closed_by,
            version=version+1
        WHERE id=:run_id;
        """,
        {
            "org1": ORG_1,
            "incident_id": INCIDENT,
            "item_id": ITEM_INCIDENT,
            "run_id": RUN_INCIDENT,
            "sha_a": SHA_A,
            "closed_by": CLOSE_ACTOR,
        },
    )


@pytest.mark.asyncio
async def test_pay14_settlement_missing_retries_and_cannot_close_or_mutate_money():
    await cleanup_phase1_tables()
    await seed_organizations()
    await _seed_run(RUN_RETRY, "pay14-retry-2026-09")

    await _insert_evidence(
        run_id=RUN_RETRY,
        evidence_id=EV_R_LOCAL,
        side="local",
        amount_minor=11800,
        evidence_kind="local_snapshot",
        sha=SHA_A,
        object_ref="pay_pay14_retry",
    )
    await _insert_evidence(
        run_id=RUN_RETRY,
        evidence_id=EV_R_PROVIDER,
        side="provider",
        amount_minor=11800,
        evidence_kind="provider_api",
        sha=SHA_B,
        object_ref="pay_pay14_retry",
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_accounting_closure_runs
        SET status='reconciling', version=version+1
        WHERE id=:run_id;

        INSERT INTO platform_accounting_reconciliation_items (
            id, closure_run_id, organization_id,
            object_type, reconciliation_key,
            local_evidence_id, provider_evidence_id,
            mismatch_category, safe_outcome, resolution_status,
            authoritative_evidence_complete,
            automatic_financial_mutation_allowed,
            reason_code,
            first_detected_at, last_checked_at
        ) VALUES (
            :item_id, :run_id, :org1,
            'captured_payment',
            'fake|test|captured_payment|pay_pay14_retry',
            :ev_local, :ev_provider,
            'settlement_missing', 'retry_required', 'retry_pending',
            false, false,
            'PAY14_SETTLEMENT_MISSING',
            '2026-09-30T14:00:00Z',
            '2026-09-30T14:00:00Z'
        );
        """,
        {
            "org1": ORG_1,
            "run_id": RUN_RETRY,
            "item_id": ITEM_RETRY,
            "ev_local": EV_R_LOCAL,
            "ev_provider": EV_R_PROVIDER,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_closure_runs
        SET observed_object_count=1,
            mismatch_count=1,
            retry_count=1,
            manual_review_count=0,
            incident_count=0,
            resolved_count=0,
            evidence_manifest_sha256=:sha_a,
            evidence_manifest_ref='evidence://pay14/manifest/retry',
            ready_at='2026-10-01T00:05:00Z',
            status='ready_to_close'
        WHERE id=:run_id
        """,
        {"org1": ORG_1, "run_id": RUN_RETRY, "sha_a": SHA_A},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_accounting_reconciliation_items
        SET automatic_financial_mutation_allowed=true
        WHERE id=:item_id
        """,
        {"org1": ORG_1, "item_id": ITEM_RETRY},
    )
