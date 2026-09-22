"""PAY-20 bound Finance delivery backlog to actionable product events.

Revision ID: zz47d8e9f0a64
Revises: zz37d8e9f0a63
Create Date: 2026-09-22

PAY-20 does not add money authority. It narrows only the PAY-5 worker claim to
the authoritative member-subscription delivery obligation: a paid Finance
invoice that has a PAY-4 member-subscription Finance binding.

PAY-18's global Finance outbox backlog definition is intentionally preserved:
all pending/processing/failed Finance outbox rows remain observable. Unbound
Finance events remain durable history and are never silently hidden, published,
or discarded.
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
_BINDING_HELPER = (
    "app_secure.pay20_member_subscription_binding_exists(uuid,uuid)"
)


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


def _install_binding_helper() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay20_member_subscription_binding_exists(
                p_organization_id uuid,
                p_invoice_id uuid
            )
            RETURNS boolean
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_previous_org text;
                v_bound boolean := false;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-20 binding eligibility requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_organization_id IS NULL OR p_invoice_id IS NULL THEN
                    RETURN false;
                END IF;

                v_previous_org :=
                    pg_catalog.current_setting('app.current_org_id',true);
                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    p_organization_id::text,
                    true
                );
                SELECT EXISTS(
                    SELECT 1
                    FROM finance.member_subscription_finance_bindings b
                    WHERE b.organization_id=p_organization_id
                      AND b.finance_invoice_id=p_invoice_id
                )
                INTO v_bound;
                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    COALESCE(v_previous_org,''),
                    true
                );
                RETURN v_bound;
            EXCEPTION WHEN OTHERS THEN
                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    COALESCE(v_previous_org,''),
                    true
                );
                RAISE;
            END
            $function$
            """
        )
        op.execute(
            "REVOKE ALL ON FUNCTION "
            "app_secure.pay20_member_subscription_binding_exists(uuid,uuid) "
            "FROM PUBLIC"
        )
    finally:
        op.execute("RESET ROLE")


def _drop_binding_helper() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            "DROP FUNCTION "
            "app_secure.pay20_member_subscription_binding_exists(uuid,uuid)"
        )
    finally:
        op.execute("RESET ROLE")


def _claim_sql(*, bound_only: bool) -> str:
    binding_predicate = (
        """
                      AND app_secure.pay20_member_subscription_binding_exists(
                          e.organization_id,
                          e.aggregate_id
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


def _replace_claim_function(*, bounded: bool) -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(_claim_sql(bound_only=bounded))
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
    helper = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='pay20_member_subscription_binding_exists'
              AND pg_catalog.oidvectortypes(p.proargtypes)='uuid, uuid'
            """
        )
    ).scalar_one()
    helper_worker_execute = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_function_privilege(
                'worker_runtime',p.oid,'EXECUTE'
            )
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='pay20_member_subscription_binding_exists'
              AND pg_catalog.oidvectortypes(p.proargtypes)='uuid, uuid'
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
    helper_name = "pay20_member_subscription_binding_exists"
    predicate = "member_subscription_finance_bindings"
    if helper_name not in claim:
        raise RuntimeError(
            "PAY-20 delivery eligibility helper missing from claim function"
        )
    if predicate not in helper or "app.current_org_id" not in helper:
        raise RuntimeError(
            "PAY-20 binding helper does not preserve tenant-scoped RLS lookup"
        )
    if helper_worker_execute:
        raise RuntimeError(
            "PAY-20 binding helper must not be directly executable by worker_runtime"
        )
    if predicate in snapshot or helper_name in snapshot:
        raise RuntimeError(
            "PAY-20 must not narrow PAY-18 global Finance outbox observability"
        )
    if "finance.outbox_events" not in snapshot or "pending" not in snapshot \
       or "processing" not in snapshot or "failed" not in snapshot:
        raise RuntimeError(
            "PAY-20 detected PAY-18 Finance outbox snapshot contract drift"
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
        _install_binding_helper()
        _replace_claim_function(bounded=True)
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
        _replace_claim_function(bounded=False)
        _drop_binding_helper()
    finally:
        if not had_create:
            op.execute(
                "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
            )
