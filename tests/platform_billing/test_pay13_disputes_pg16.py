from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from tests.platform_billing.test_pay11_production_model import (
    CATALOG_RELEASE_1,
    PROVIDER_RELEASE_1,
    _seed_pay11_contract,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    ORG_2,
    SHA_A,
    SHA_B,
    exec_sql,
    expect_db_error,
)


INVOICE_SEQUENCE = "94000000-0000-0000-0000-000000000001"
INVOICE = "94000000-0000-0000-0000-000000000101"
INVOICE_LINE = "94000000-0000-0000-0000-000000000102"
PAYMENT = "94000000-0000-0000-0000-000000000201"
FAILED_PAYMENT = "94000000-0000-0000-0000-000000000202"
DISPUTE = "94000000-0000-0000-0000-000000000301"
EVIDENCE = "94000000-0000-0000-0000-000000000401"
EVENT_OPEN = "94000000-0000-0000-0000-000000000501"
EVENT_DECISION = "94000000-0000-0000-0000-000000000502"
LIABILITY = "94000000-0000-0000-0000-000000000601"
DECISION_ENTRY = "94000000-0000-0000-0000-000000000602"
EXCEPTION = "94000000-0000-0000-0000-000000000701"


async def _seed_captured_payment(
    *,
    invoice_id: str = INVOICE,
    payment_id: str = PAYMENT,
    external_payment_ref: str = "fake_payment_pay13",
) -> dict[str, str]:
    ids = await _seed_pay11_contract()
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

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
            11800, :catalog_release, :provider_release,
            :plan, :price, :sha_a,
            '2026-10-01T00:00:00Z', '2026-11-01T00:00:00Z',
            '{"gst":"18pct"}'::jsonb, '{"country":"IN"}'::jsonb
        );

        INSERT INTO platform_document_sequences (
            id, legal_entity_code, document_type, financial_year,
            series_code, prefix, padding, last_number, status
        ) VALUES (
            :invoice_sequence, 'DOERS_IN', 'invoice', '2026-27',
            'PAY13', 'INV-P13-', 6, 1, 'active'
        );

        INSERT INTO platform_invoice_lines (
            id, organization_id, invoice_id, line_number, line_type,
            description, quantity, unit_amount_minor, net_amount_minor,
            tax_rate_bps, tax_amount_minor, gross_amount_minor,
            plan_version_id, price_id
        ) VALUES (
            :invoice_line, :org1, :invoice, 1, 'subscription',
            'PAY-13 captured payment fixture', 1, 10000, 10000,
            1800, 1800, 11800, :plan, :price
        );

        UPDATE platform_invoices
        SET status='issued',
            document_sequence_id=:invoice_sequence,
            invoice_number='INV-P13-000001',
            issued_at='2026-10-01T00:00:00Z'
        WHERE id=:invoice;

        INSERT INTO platform_payment_attempts (
            id, organization_id, invoice_id, provider_release_id,
            provider_code, external_payment_ref, idempotency_key,
            attempt_number, amount_minor, currency_code, status,
            provider_evidence_sha256, provider_evidence_ref, completed_at
        ) VALUES (
            :payment, :org1, :invoice, :provider_release,
            'fake', :external_payment_ref, :payment_idempotency,
            1, 11800, 'INR', 'succeeded',
            :sha_b, 'evidence://pay13/capture', '2026-10-01T00:01:00Z'
        );
        """,
        ids
        | {
            "org1": ORG_1,
            "invoice_sequence": INVOICE_SEQUENCE,
            "invoice": invoice_id,
            "invoice_line": INVOICE_LINE,
            "payment": payment_id,
            "catalog_release": CATALOG_RELEASE_1,
            "provider_release": PROVIDER_RELEASE_1,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
            "external_payment_ref": external_payment_ref,
            "payment_idempotency": f"pay13-{payment_id}",
        },
    )
    return ids


async def _open_dispute(*, dispute_type: str = "chargeback") -> dict[str, str]:
    ids = await _seed_captured_payment()
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_disputes (
            id, organization_id, payment_attempt_id, invoice_id,
            provider_release_id, provider_code, environment,
            external_dispute_ref, dispute_type, status,
            amount_minor, currency_code, reason_code,
            financial_hold_active, last_evidence_sha256,
            last_evidence_ref, opened_at
        ) VALUES (
            :dispute, :org1, :payment, :invoice,
            :provider_release, 'fake', 'test',
            'dp_pay13_1', :dispute_type, 'opened',
            11800, 'INR', 'provider_dispute',
            true, :sha_a, 'evidence://pay13/dispute/open',
            '2026-10-02T00:00:00Z'
        );

        INSERT INTO platform_dispute_financial_entries (
            id, organization_id, dispute_id, payment_attempt_id,
            entry_type, amount_minor, currency_code,
            evidence_sha256, evidence_ref, effective_at
        ) VALUES (
            :liability, :org1, :dispute, :payment,
            'liability_recognized', 11800, 'INR',
            :sha_a, 'evidence://pay13/dispute/open',
            '2026-10-02T00:00:00Z'
        );

        INSERT INTO platform_dispute_events (
            id, organization_id, dispute_id, sequence_number,
            event_type, from_status, to_status, source_type,
            evidence_sha256, occurred_at, payload_json, payload_sha256
        ) VALUES (
            :event_open, :org1, :dispute, 1,
            'dispute_opened', NULL, 'opened', 'webhook',
            :sha_a, '2026-10-02T00:00:00Z',
            '{"source":"provider"}'::jsonb, :sha_b
        );
        """,
        {
            "org1": ORG_1,
            "dispute": DISPUTE,
            "payment": PAYMENT,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
            "dispute_type": dispute_type,
            "liability": LIABILITY,
            "event_open": EVENT_OPEN,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
        },
    )
    return ids


@pytest.mark.asyncio
async def test_pay13_open_dispute_preserves_capture_and_freezes_money_actions():
    ids = await _open_dispute()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        payment_status = (
            await session.execute(
                text("SELECT status FROM platform_payment_attempts WHERE id=:payment"),
                {"payment": PAYMENT},
            )
        ).scalar_one()
        assert payment_status == "succeeded"

        dispute = (
            await session.execute(
                text(
                    """
                    SELECT status, financial_hold_active
                    FROM platform_disputes
                    WHERE id=:dispute
                    """
                ),
                {"dispute": DISPUTE},
            )
        ).one()
        assert tuple(dispute) == ("opened", True)

        liability_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM platform_dispute_financial_entries
                    WHERE dispute_id=:dispute
                      AND entry_type='liability_recognized'
                    """
                ),
                {"dispute": DISPUTE},
            )
        ).scalar_one()
        assert liability_count == 1

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
        INSERT INTO platform_payment_attempts (
            organization_id, invoice_id, provider_release_id,
            provider_code, external_payment_ref, idempotency_key,
            attempt_number, amount_minor, currency_code, status
        ) VALUES (
            :org1, :invoice, :provider_release,
            'fake', 'fake_payment_pay13_illegal_second',
            'pay13-illegal-second-charge', 2,
            11800, 'INR', 'processing'
        )
        """,
        {
            "org1": ORG_1,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
        },
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
            :provider_release, 'fake', 'pay13-frozen-refund',
            11800, 'INR', 'dispute_open', 'requested'
        )
        """,
        {
            "org1": ORG_1,
            "payment": PAYMENT,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
        },
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org2, true)"),
            {"org2": ORG_2},
        )
        assert (
            await session.execute(
                text("SELECT count(*) FROM platform_disputes WHERE id=:dispute"),
                {"dispute": DISPUTE},
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
async def test_pay13_open_requires_captured_payment_and_atomic_liability():
    ids = await _seed_captured_payment()

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_disputes (
            organization_id, payment_attempt_id, invoice_id,
            provider_release_id, provider_code, environment,
            external_dispute_ref, dispute_type, status,
            amount_minor, currency_code, financial_hold_active,
            last_evidence_sha256, last_evidence_ref, opened_at
        ) VALUES (
            :org1, :payment, :invoice,
            :provider_release, 'fake', 'test',
            'dp_missing_liability', 'chargeback', 'opened',
            11800, 'INR', true,
            :sha_a, 'evidence://pay13/missing-liability',
            '2026-10-02T00:00:00Z'
        )
        """,
        {
            "org1": ORG_1,
            "payment": PAYMENT,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
            "sha_a": SHA_A,
        },
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_payment_attempts (
            id, organization_id, invoice_id, provider_release_id,
            provider_code, external_payment_ref, idempotency_key,
            attempt_number, amount_minor, currency_code, status,
            failure_classification, completed_at
        ) VALUES (
            :failed_payment, :org1, :invoice, :provider_release,
            'fake', 'fake_payment_pay13_failed',
            'pay13-failed-payment-proof', 2,
            11800, 'INR', 'failed',
            'provider_declined', '2026-10-02T00:00:00Z'
        )
        """,
        {
            "org1": ORG_1,
            "failed_payment": FAILED_PAYMENT,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_disputes (
            organization_id, payment_attempt_id, invoice_id,
            provider_release_id, provider_code, environment,
            external_dispute_ref, dispute_type, status,
            amount_minor, currency_code, financial_hold_active,
            last_evidence_sha256, last_evidence_ref, opened_at
        ) VALUES (
            :org1, :failed_payment, :invoice,
            :provider_release, 'fake', 'test',
            'dp_failed_payment', 'chargeback', 'opened',
            11800, 'INR', true,
            :sha_a, 'evidence://pay13/failed-payment',
            '2026-10-02T00:00:00Z'
        )
        """,
        {
            "org1": ORG_1,
            "failed_payment": FAILED_PAYMENT,
            "invoice": INVOICE,
            "provider_release": PROVIDER_RELEASE_1,
            "sha_a": SHA_A,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_status", "financial_entry_type", "decision_ref"),
    [
        ("won", "liability_reversed", "decision-pay13-win"),
        ("lost", "loss_recognized", "decision-pay13-loss"),
    ],
)
async def test_pay13_provider_decision_requires_same_transaction_financial_closure(
    target_status: str,
    financial_entry_type: str,
    decision_ref: str,
):
    await _open_dispute()

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_dispute_evidence (
            id, organization_id, dispute_id, evidence_kind,
            evidence_sha256, evidence_ref, source_type,
            metadata_json, observed_at, submitted_at
        ) VALUES (
            :evidence, :org1, :dispute, 'service_delivery',
            :sha_b, 'evidence://pay13/service-delivery',
            'manual_review', '{"safe":true}'::jsonb,
            '2026-10-03T00:00:00Z', '2026-10-03T00:00:00Z'
        );

        UPDATE platform_disputes
        SET status='submitted',
            submitted_at='2026-10-03T00:00:00Z',
            last_evidence_sha256=:sha_b,
            last_evidence_ref='evidence://pay13/service-delivery',
            version=version+1
        WHERE id=:dispute;

        UPDATE platform_disputes
        SET status='under_review',
            version=version+1
        WHERE id=:dispute;
        """,
        {
            "org1": ORG_1,
            "evidence": EVIDENCE,
            "dispute": DISPUTE,
            "sha_b": SHA_B,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_disputes
        SET status=:target_status,
            provider_decision_ref=:decision_ref,
            decided_at='2026-10-04T00:00:00Z',
            financial_hold_active=false,
            hold_released_at='2026-10-04T00:00:00Z',
            version=version+1
        WHERE id=:dispute
        """,
        {
            "org1": ORG_1,
            "target_status": target_status,
            "decision_ref": decision_ref,
            "dispute": DISPUTE,
        },
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_disputes
        SET status=:target_status,
            provider_decision_ref=:decision_ref,
            decided_at='2026-10-04T00:00:00Z',
            financial_hold_active=false,
            hold_released_at='2026-10-04T00:00:00Z',
            version=version+1
        WHERE id=:dispute;

        INSERT INTO platform_dispute_financial_entries (
            id, organization_id, dispute_id, payment_attempt_id,
            entry_type, amount_minor, currency_code,
            evidence_sha256, evidence_ref, effective_at
        ) VALUES (
            :decision_entry, :org1, :dispute, :payment,
            :financial_entry_type, 11800, 'INR',
            :sha_b, 'evidence://pay13/provider-decision',
            '2026-10-04T00:00:00Z'
        );

        INSERT INTO platform_dispute_events (
            id, organization_id, dispute_id, sequence_number,
            event_type, from_status, to_status, source_type,
            evidence_sha256, occurred_at, payload_json, payload_sha256
        ) VALUES (
            :event_decision, :org1, :dispute, 2,
            :event_type, 'under_review', :target_status, 'webhook',
            :sha_b, '2026-10-04T00:00:00Z',
            '{"provider_decision":true}'::jsonb, :sha_a
        );
        """,
        {
            "org1": ORG_1,
            "target_status": target_status,
            "decision_ref": decision_ref,
            "dispute": DISPUTE,
            "decision_entry": DECISION_ENTRY,
            "payment": PAYMENT,
            "financial_entry_type": financial_entry_type,
            "sha_a": SHA_A,
            "sha_b": SHA_B,
            "event_decision": EVENT_DECISION,
            "event_type": f"dispute_{target_status}",
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
                    SELECT status, financial_hold_active
                    FROM platform_disputes
                    WHERE id=:dispute
                    """
                ),
                {"dispute": DISPUTE},
            )
        ).one()
        assert tuple(row) == (target_status, False)


@pytest.mark.asyncio
async def test_pay13_chargeback_reversal_is_append_only_after_recorded_loss():
    await _open_dispute()
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        UPDATE platform_disputes
        SET status='lost',
            provider_decision_ref='decision-pay13-loss',
            decided_at='2026-10-04T00:00:00Z',
            financial_hold_active=false,
            hold_released_at='2026-10-04T00:00:00Z',
            last_evidence_sha256=:sha_b,
            last_evidence_ref='evidence://pay13/loss',
            version=version+1
        WHERE id=:dispute;

        INSERT INTO platform_dispute_financial_entries (
            organization_id, dispute_id, payment_attempt_id,
            entry_type, amount_minor, currency_code,
            evidence_sha256, evidence_ref, effective_at
        ) VALUES (
            :org1, :dispute, :payment,
            'loss_recognized', 11800, 'INR',
            :sha_b, 'evidence://pay13/loss',
            '2026-10-04T00:00:00Z'
        );
        """,
        {
            "org1": ORG_1,
            "dispute": DISPUTE,
            "payment": PAYMENT,
            "sha_b": SHA_B,
        },
    )

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_dispute_financial_entries (
            organization_id, dispute_id, payment_attempt_id,
            entry_type, amount_minor, currency_code,
            evidence_sha256, evidence_ref, effective_at
        ) VALUES (
            :org1, :dispute, :payment,
            'loss_reversed', 11800, 'INR',
            :sha_a, 'evidence://pay13/chargeback-reversal',
            '2026-10-10T00:00:00Z'
        );
        """,
        {
            "org1": ORG_1,
            "dispute": DISPUTE,
            "payment": PAYMENT,
            "sha_a": SHA_A,
        },
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        status = (
            await session.execute(
                text("SELECT status FROM platform_disputes WHERE id=:dispute"),
                {"dispute": DISPUTE},
            )
        ).scalar_one()
        entries = (
            await session.execute(
                text(
                    """
                    SELECT entry_type
                    FROM platform_dispute_financial_entries
                    WHERE dispute_id=:dispute
                    ORDER BY created_at, entry_type
                    """
                ),
                {"dispute": DISPUTE},
            )
        ).scalars().all()
        assert status == "lost"
        assert "loss_recognized" in entries
        assert "loss_reversed" in entries


@pytest.mark.asyncio
async def test_pay13_exception_case_is_manual_review_and_no_auto_money_mutation():
    await _seed_captured_payment()

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        INSERT INTO platform_financial_exception_cases (
            id, organization_id, provider_code, environment,
            exception_type, status, severity,
            external_object_type, external_object_ref,
            amount_minor, currency_code,
            initial_evidence_sha256, initial_evidence_ref,
            source_type, manual_review_required,
            automatic_financial_mutation_allowed, detected_at
        ) VALUES (
            :exception, :org1, 'fake', 'test',
            'wrong_customer_mapping', 'quarantined', 'critical',
            'payment', 'pay_wrong_customer',
            11800, 'INR',
            :sha_a, 'evidence://pay13/wrong-customer',
            'reconciliation', true, false,
            '2026-10-05T00:00:00Z'
        );
        """,
        {
            "exception": EXCEPTION,
            "org1": ORG_1,
            "sha_a": SHA_A,
        },
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_financial_exception_cases
        SET automatic_financial_mutation_allowed=true
        WHERE id=:exception
        """,
        {"org1": ORG_1, "exception": EXCEPTION},
    )

    await expect_db_error(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        UPDATE platform_financial_exception_cases
        SET manual_review_required=false
        WHERE id=:exception
        """,
        {"org1": ORG_1, "exception": EXCEPTION},
    )
