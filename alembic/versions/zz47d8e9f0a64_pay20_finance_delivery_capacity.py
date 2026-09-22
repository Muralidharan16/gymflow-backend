"""PAY-20 bound Finance delivery backlog to actionable product events.

Revision ID: zz47d8e9f0a64
Revises: zz37d8e9f0a63
Create Date: 2026-09-22

PAY-20 does not add money authority. It narrows the PAY-5 worker claim and
PAY-18 Finance outbox backlog to the same authoritative delivery obligation:
a paid Finance invoice that has a PAY-4 member-subscription Finance binding.

Unbound Finance events remain durable history and are never silently marked
published or discarded.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz47d8e9f0a64"
down_revision = "zz37d8e9f0a63"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text,current_user::text,
                   pg_catalog.pg_has_role(
                       'migration_owner','app_security_owner','SET'
                   )
            """
        )
    ).one()
    if tuple(row[:2]) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-20 migration requires migration_owner")
    if row[2] is not True:
        raise RuntimeError(
            "PAY-20 migration_owner requires SET edge to app_security_owner"
        )


def _claim_sql(*, bound_only: bool) -> str:
    binding_predicate = (
        """
                      AND EXISTS (
                          SELECT 1
                          FROM finance.member_subscription_finance_bindings b
                          WHERE b.organization_id=e.organization_id
                            AND b.finance_invoice_id=e.aggregate_id
                      )
        """
        if bound_only
        else ""
    )
    return f"""
    CREATE OR REPLACE FUNCTION app_secure.claim_member_subscription_finance_events(
        p_worker_id uuid,
        p_batch_size integer,
        p_lease_seconds integer
    )
    RETURNS TABLE(
        finance_event_id uuid,
        organization_id uuid,
        idempotency_key text,
        attempt_count integer,
        max_attempts integer,
        lease_fence bigint
    )
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path=pg_catalog,public,finance
    SET row_security=on
    AS $function$
    BEGIN
        IF NOT pg_catalog.pg_has_role(
            session_user,'worker_runtime','MEMBER'
        ) THEN
            RAISE EXCEPTION
                'PAY-5 Finance-event claim requires worker_runtime'
                USING ERRCODE='42501';
        END IF;
        IF p_worker_id IS NULL
           OR p_batch_size IS NULL
           OR p_batch_size < 1 OR p_batch_size > 100
           OR p_lease_seconds IS NULL
           OR p_lease_seconds < 30 OR p_lease_seconds > 3600
        THEN
            RAISE EXCEPTION 'PAY-5 Finance-event claim arguments invalid'
                USING ERRCODE='22023';
        END IF;

        RETURN QUERY
        WITH candidates AS (
            SELECT e.id,
                   (e.status='processing') AS reclaiming
            FROM finance.outbox_events e
            WHERE e.organization_id IS NOT NULL
              AND e.aggregate_type='invoice'
              AND e.event_type='finance.invoice.paid'
              {binding_predicate}
              AND (
                (
                  e.status='pending'
                  AND e.attempt_count < e.max_attempts
                )
                OR (
                  e.status='processing'
                  AND e.leased_until <= pg_catalog.clock_timestamp()
                )
              )
            ORDER BY e.created_at,e.id
            LIMIT p_batch_size
            FOR UPDATE OF e SKIP LOCKED
        )
        UPDATE finance.outbox_events e
        SET status='processing',
            attempt_count=CASE
                WHEN c.reclaiming THEN e.attempt_count
                ELSE e.attempt_count+1
            END,
            claimed_at=pg_catalog.clock_timestamp(),
            leased_by=p_worker_id,
            leased_until=pg_catalog.clock_timestamp()
                +(p_lease_seconds*INTERVAL '1 second'),
            lease_fence=e.lease_fence+1,
            last_error_code=NULL
        FROM candidates c
        WHERE e.id=c.id
        RETURNING e.id,e.organization_id,e.idempotency_key::text,
                  e.attempt_count,e.max_attempts,e.lease_fence;
    END
    $function$
    """


def _snapshot_sql(*, dispatchable_only: bool) -> str:
    outbox_predicate = (
        """
                      AND e.aggregate_type='invoice'
                      AND e.event_type='finance.invoice.paid'
                      AND EXISTS (
                          SELECT 1
                          FROM finance.member_subscription_finance_bindings b
                          WHERE b.organization_id=e.organization_id
                            AND b.finance_invoice_id=e.aggregate_id
                      )
        """
        if dispatchable_only
        else ""
    )
    return f"""
    CREATE OR REPLACE FUNCTION app_secure.pay18_financial_observability_snapshot()
    RETURNS TABLE(
        payment_attempt_total bigint,
        payment_failure_total bigint,
        payment_unknown_total bigint,
        webhook_backlog bigint,
        payment_application_backlog bigint,
        finance_outbox_backlog bigint,
        platform_refund_backlog bigint,
        platform_refund_unknown_total bigint,
        settlement_mismatch_total bigint,
        reconciliation_open_total bigint,
        mandate_failed_total bigint,
        mandate_expired_total bigint,
        mandate_revoked_total bigint,
        dunning_full_grace_total bigint,
        dunning_limited_write_total bigint,
        dunning_read_only_total bigint,
        dunning_billing_only_total bigint,
        dunning_recovered_total bigint,
        chargeback_open_total bigint,
        duplicate_payment_allegation_open_total bigint
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path=pg_catalog,public,finance
    SET row_security=on
    AS $function$
        WITH payment_attempts AS (
            SELECT
                count(*)::bigint AS total,
                count(*) FILTER (WHERE status='failed')::bigint AS failed,
                count(*) FILTER (WHERE status='unknown')::bigint
                    AS unknown_count
            FROM public.platform_payment_attempts
        ),
        webhook AS (
            SELECT count(*)::bigint AS backlog
            FROM finance.provider_webhook_inbox
            WHERE status IN ('received','processing','retry','dead_letter')
        ),
        application AS (
            SELECT count(*)::bigint AS backlog
            FROM finance.payment_events AS event_data
            WHERE event_data.event_type IN ('payment.captured','order.paid')
              AND NOT EXISTS (
                SELECT 1
                FROM finance.payment_application_records AS applied
                WHERE applied.payment_event_id=event_data.id
              )
        ),
        outbox AS (
            SELECT count(*)::bigint AS backlog
            FROM finance.outbox_events e
            WHERE e.status IN ('pending','processing','failed')
              {outbox_predicate}
        ),
        platform_refunds AS (
            SELECT
                count(*) FILTER (
                    WHERE status IN (
                        'requested','approved','provider_pending','unknown'
                    )
                )::bigint AS backlog,
                count(*) FILTER (WHERE status='unknown')::bigint
                    AS unknown_count
            FROM public.platform_refunds
        ),
        reconciliation AS (
            SELECT
                count(*) FILTER (
                    WHERE mismatch_category IS NOT NULL
                      AND resolution_status<>'resolved'
                      AND (
                          object_type IN (
                              'settlement','gateway_fee','refund_fee'
                          )
                          OR settlement_evidence_id IS NOT NULL
                          OR mismatch_category='settlement_missing'
                      )
                )::bigint AS settlement_mismatch,
                count(*) FILTER (
                    WHERE resolution_status<>'resolved'
                )::bigint AS open_count
            FROM public.platform_accounting_reconciliation_items
        ),
        mandates AS (
            SELECT
                count(*) FILTER (
                    WHERE status='failed'
                      AND replacement_mandate_id IS NULL
                )::bigint AS failed,
                count(*) FILTER (
                    WHERE status='expired'
                      AND replacement_mandate_id IS NULL
                )::bigint AS expired,
                count(*) FILTER (
                    WHERE status='revoked'
                      AND replacement_mandate_id IS NULL
                )::bigint AS revoked
            FROM public.platform_mandates
        ),
        dunning AS (
            SELECT
                count(*) FILTER (
                    WHERE stage='full_grace'
                      AND status IN ('open','suspended')
                )::bigint AS full_grace,
                count(*) FILTER (
                    WHERE stage='limited_write'
                      AND status IN ('open','suspended')
                )::bigint AS limited_write,
                count(*) FILTER (
                    WHERE stage='read_only'
                      AND status IN ('open','suspended')
                )::bigint AS read_only_count,
                count(*) FILTER (
                    WHERE stage='billing_only'
                      AND status IN ('open','suspended')
                )::bigint AS billing_only,
                count(*) FILTER (WHERE stage='recovered')::bigint
                    AS recovered
            FROM public.platform_dunning_cases
        ),
        disputes AS (
            SELECT
                count(*) FILTER (
                    WHERE dispute_type='chargeback'
                      AND status IN (
                          'opened','evidence_required','submitted','under_review'
                      )
                )::bigint AS chargeback_open,
                count(*) FILTER (
                    WHERE dispute_type='duplicate_charge_allegation'
                      AND status IN (
                          'opened','evidence_required','submitted','under_review'
                      )
                )::bigint AS duplicate_open
            FROM public.platform_disputes
        )
        SELECT
            p.total,p.failed,p.unknown_count,w.backlog,a.backlog,o.backlog,
            r.backlog,r.unknown_count,rec.settlement_mismatch,rec.open_count,
            m.failed,m.expired,m.revoked,d.full_grace,d.limited_write,
            d.read_only_count,d.billing_only,d.recovered,
            dp.chargeback_open,dp.duplicate_open
        FROM payment_attempts p
        CROSS JOIN webhook w
        CROSS JOIN application a
        CROSS JOIN outbox o
        CROSS JOIN platform_refunds r
        CROSS JOIN reconciliation rec
        CROSS JOIN mandates m
        CROSS JOIN dunning d
        CROSS JOIN disputes dp
    $function$
    """


def _replace_functions(*, bounded: bool) -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(_claim_sql(bound_only=bounded))
        op.execute(_snapshot_sql(dispatchable_only=bounded))
    finally:
        op.execute("RESET ROLE")


def _postflight(bind) -> None:
    claim = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='claim_member_subscription_finance_events'
              AND pg_catalog.oidvectortypes(p.proargtypes)
                    = 'uuid, integer, integer'
            """
        )
    ).scalar_one()
    snapshot = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='pay18_financial_observability_snapshot'
              AND p.pronargs=0
            """
        )
    ).scalar_one()
    predicate = "member_subscription_finance_bindings"
    if predicate not in claim or predicate not in snapshot:
        raise RuntimeError(
            "PAY-20 delivery eligibility predicate missing from runtime functions"
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    )
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app_secure TO app_security_owner")
    try:
        _replace_functions(bounded=True)
    finally:
        if not had_create:
            op.execute(
                "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
            )
    _postflight(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    had_create = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                "'app_security_owner','app_secure','CREATE')"
            )
        ).scalar_one()
    )
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app_secure TO app_security_owner")
    try:
        _replace_functions(bounded=False)
    finally:
        if not had_create:
            op.execute(
                "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
            )
