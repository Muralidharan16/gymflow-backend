"""PAY-5 durable Finance event delivery.

Revision ID: zp07d8e9f0a50
Revises: zo07d8e9f0a49
Create Date: 2026-09-19

Adds leased/fenced delivery metadata to finance.outbox_events and a
product-owned member-subscription Finance-event consumption journal. Runtime
workers receive only bounded SECURITY DEFINER capabilities: claim, consume,
acknowledge and release. The PAY-4 activation capability remains non-directly
executable by worker_runtime.

No provider interaction, live money movement, refund execution, release or
deployment is introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zp07d8e9f0a50"
down_revision = "zo07d8e9f0a49"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_OUTBOX = "finance.outbox_events"
_CONSUMPTIONS = "public.member_subscription_finance_event_consumptions"

_CLAIM = "app_secure.claim_member_subscription_finance_events(uuid,integer,integer)"
_CONSUME = "app_secure.consume_member_subscription_finance_event(uuid,uuid,bigint)"
_ACK = "app_secure.acknowledge_member_subscription_finance_event(uuid,uuid,bigint)"
_RELEASE = "app_secure.release_member_subscription_finance_event(uuid,uuid,bigint,text,boolean)"
_PAY4_APPLY = "app_secure.apply_member_subscription_finance_event(uuid,text)"

_TENANT = "NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid"


def _require_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role
            """
        ),
        {"role": role},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-5 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-5 role login posture drift: {role}")
    for key in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-5 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _WORKER)
    identity=bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER,_MIGRATION_OWNER):
        raise RuntimeError("PAY-5 migration requires migration_owner")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member":_MIGRATION_OWNER,"target":_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-5 requires migration_owner SET edge to app_security_owner")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
            "OR pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member":_WORKER,"target":_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-5 worker_runtime can reach app_security_owner")


def _install_outbox_delivery_columns() -> None:
    op.execute(
        """
        ALTER TABLE finance.outbox_events
          ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 15,
          ADD COLUMN leased_by UUID NULL,
          ADD COLUMN leased_until TIMESTAMPTZ NULL,
          ADD COLUMN lease_fence BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE finance.outbox_events
          ADD CONSTRAINT chk_finance_outbox_events_max_attempts
            CHECK (max_attempts > 0),
          ADD CONSTRAINT chk_finance_outbox_events_lease_fence
            CHECK (lease_fence >= 0),
          ADD CONSTRAINT chk_finance_outbox_events_lease_pair
            CHECK ((leased_by IS NULL) = (leased_until IS NULL))
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_outbox_events_pay5_claim
        ON finance.outbox_events(status,leased_until,created_at,id)
        WHERE aggregate_type='invoice'
          AND event_type='finance.invoice.paid'
          AND status IN ('pending','processing')
        """
    )

    # PAY-4/P4D already own the event identity, tenant and payload read columns.
    # PAY-5 grants only its predecessor-absent delivery columns.
    op.execute(
        "GRANT SELECT (idempotency_key,payload_sha256,status,attempt_count,"
        "created_at,max_attempts,leased_by,leased_until,lease_fence) "
        "ON TABLE finance.outbox_events TO app_security_owner"
    )
    op.execute(
        "GRANT UPDATE (status,attempt_count,claimed_at,published_at,"
        "acknowledged_at,last_error_code,leased_by,leased_until,lease_fence) "
        "ON TABLE finance.outbox_events TO app_security_owner"
    )


def _install_consumption_table() -> None:
    op.execute(
        """
        CREATE TABLE public.member_subscription_finance_event_consumptions (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            org_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            finance_event_id UUID NOT NULL
                REFERENCES finance.outbox_events(id) ON DELETE RESTRICT,
            idempotency_key VARCHAR(200) NOT NULL,
            finance_payload_sha256 CHAR(64) NOT NULL,
            subscription_term_id UUID NOT NULL,
            business_result_status TEXT NOT NULL,
            effect_applied BOOLEAN NOT NULL,
            consumed_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT fk_pay5_consumption_term_org
                FOREIGN KEY (subscription_term_id,org_id)
                REFERENCES public.subscription_terms(id,org_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay5_consumption_finance_event
                UNIQUE (finance_event_id),
            CONSTRAINT uq_pay5_consumption_idempotency
                UNIQUE (idempotency_key),
            CONSTRAINT chk_pay5_consumption_payload_hash
                CHECK (finance_payload_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay5_consumption_result
                CHECK (business_result_status IN ('active','scheduled'))
        )
        """
    )
    op.execute(
        "ALTER TABLE public.member_subscription_finance_event_consumptions "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.member_subscription_finance_event_consumptions "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON TABLE public.member_subscription_finance_event_consumptions FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT,INSERT ON TABLE public.member_subscription_finance_event_consumptions "
        "TO app_security_owner"
    )
    op.execute(
        f"""
        CREATE POLICY pay5_consumption_security_owner_select
        ON public.member_subscription_finance_event_consumptions
        FOR SELECT TO app_security_owner
        USING (org_id={_TENANT})
        """
    )
    op.execute(
        f"""
        CREATE POLICY pay5_consumption_security_owner_insert
        ON public.member_subscription_finance_event_consumptions
        FOR INSERT TO app_security_owner
        WITH CHECK (org_id={_TENANT})
        """
    )


def _install_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.claim_member_subscription_finance_events(
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
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event claim requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL
                   OR p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 100
                   OR p_lease_seconds IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 3600
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
                    FOR UPDATE SKIP LOCKED
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
                RETURNING e.id,e.organization_id,e.idempotency_key,
                          e.attempt_count,e.max_attempts,e.lease_fence;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.consume_member_subscription_finance_event(
                p_finance_event_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint
            )
            RETURNS TABLE(
                subscription_term_id uuid,
                subscription_status text,
                effect_applied boolean,
                replayed boolean,
                consumption_id uuid
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_event record;
                v_consumption public.member_subscription_finance_event_consumptions%ROWTYPE;
                v_apply record;
                v_consume_key text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event consume requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL OR p_worker_id IS NULL
                   OR p_lease_fence IS NULL OR p_lease_fence < 1
                THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event consume identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.id,e.organization_id,e.aggregate_type,e.aggregate_id,
                       e.event_type,e.payload_sha256,e.status,e.leased_by,
                       e.leased_until,e.lease_fence
                INTO v_event
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND
                   OR v_event.aggregate_type<>'invoice'
                   OR v_event.event_type<>'finance.invoice.paid'
                   OR v_event.status<>'processing'
                   OR v_event.leased_by IS DISTINCT FROM p_worker_id
                   OR v_event.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_event.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event lease is not owned by this worker/fence'
                        USING ERRCODE='40001';
                END IF;

                v_consume_key:='pay5:member-subscription:'||p_finance_event_id::text;

                SELECT * INTO v_consumption
                FROM public.member_subscription_finance_event_consumptions c
                WHERE c.finance_event_id=p_finance_event_id
                   OR c.idempotency_key=v_consume_key
                FOR SHARE;
                IF FOUND THEN
                    IF v_consumption.finance_event_id IS DISTINCT FROM p_finance_event_id
                       OR v_consumption.org_id IS DISTINCT FROM v_org
                       OR v_consumption.idempotency_key IS DISTINCT FROM v_consume_key
                       OR v_consumption.finance_payload_sha256 IS DISTINCT FROM v_event.payload_sha256
                    THEN
                        RAISE EXCEPTION 'PAY-5 consumed Finance-event identity conflict'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_consumption.subscription_term_id,
                        v_consumption.business_result_status,
                        false,true,v_consumption.id;
                    RETURN;
                END IF;

                SELECT * INTO v_apply
                FROM app_secure.apply_member_subscription_finance_event(
                    p_finance_event_id,
                    v_consume_key
                );

                INSERT INTO public.member_subscription_finance_event_consumptions(
                    org_id,finance_event_id,idempotency_key,finance_payload_sha256,
                    subscription_term_id,business_result_status,effect_applied
                ) VALUES (
                    v_org,p_finance_event_id,v_consume_key,v_event.payload_sha256,
                    v_apply.subscription_term_id,v_apply.subscription_status,
                    v_apply.activated
                )
                RETURNING * INTO v_consumption;

                RETURN QUERY SELECT
                    v_consumption.subscription_term_id,
                    v_consumption.business_result_status,
                    v_consumption.effect_applied,
                    false,
                    v_consumption.id;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.acknowledge_member_subscription_finance_event(
                p_finance_event_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint
            )
            RETURNS boolean
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_consumed boolean;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event acknowledgement requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL OR p_worker_id IS NULL
                   OR p_lease_fence IS NULL OR p_lease_fence < 1
                THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event acknowledgement identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT EXISTS(
                    SELECT 1
                    FROM public.member_subscription_finance_event_consumptions c
                    WHERE c.org_id=v_org
                      AND c.finance_event_id=p_finance_event_id
                ) INTO v_consumed;
                IF NOT v_consumed THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event cannot acknowledge before product commit'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.outbox_events e
                SET status='published',
                    published_at=COALESCE(e.published_at,pg_catalog.clock_timestamp()),
                    acknowledged_at=pg_catalog.clock_timestamp(),
                    leased_by=NULL,
                    leased_until=NULL,
                    last_error_code=NULL
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='invoice'
                  AND e.event_type='finance.invoice.paid'
                  AND e.status='processing'
                  AND e.leased_by=p_worker_id
                  AND e.lease_fence=p_lease_fence
                  AND e.leased_until>pg_catalog.clock_timestamp();

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event acknowledgement lost lease/fence'
                        USING ERRCODE='40001';
                END IF;
                RETURN true;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.release_member_subscription_finance_event(
                p_finance_event_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_error_code text,
                p_permanent boolean
            )
            RETURNS text
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_event record;
                v_terminal boolean;
                v_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event release requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL OR p_worker_id IS NULL
                   OR p_lease_fence IS NULL OR p_lease_fence < 1
                   OR p_permanent IS NULL
                   OR p_error_code IS NULL OR p_error_code !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,79}$'
                THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event release arguments invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.attempt_count,e.max_attempts
                INTO v_event
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='invoice'
                  AND e.event_type='finance.invoice.paid'
                  AND e.status='processing'
                  AND e.leased_by=p_worker_id
                  AND e.lease_fence=p_lease_fence
                  AND e.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-5 Finance-event release lost lease/fence'
                        USING ERRCODE='40001';
                END IF;

                v_terminal:=p_permanent OR v_event.attempt_count>=v_event.max_attempts;
                v_status:=CASE WHEN v_terminal THEN 'failed' ELSE 'pending' END;

                UPDATE finance.outbox_events
                SET status=v_status,
                    leased_by=NULL,
                    leased_until=NULL,
                    last_error_code=p_error_code
                WHERE id=p_finance_event_id;

                RETURN v_status;
            END
            $function$
            """
        )

        for signature in (_CLAIM,_CONSUME,_ACK,_RELEASE):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime")
    finally:
        op.execute("RESET ROLE")


def upgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:name) IS NOT NULL"),
        {"name":_CONSUMPTIONS},
    ).scalar_one():
        raise RuntimeError("PAY-5 consumption relation already exists")

    for column_name in ("max_attempts","leased_by","leased_until","lease_fence"):
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM information_schema.columns
                  WHERE table_schema='finance'
                    AND table_name='outbox_events'
                    AND column_name=:column_name
                )
                """
            ),
            {"column_name":column_name},
        ).scalar_one():
            raise RuntimeError(f"PAY-5 outbox column already exists: {column_name}")

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_function_privilege("
            "'worker_runtime','app_secure.apply_member_subscription_finance_event(uuid,text)','EXECUTE')"
        )
    ).scalar_one():
        raise RuntimeError("PAY-5 refuses direct worker EXECUTE on PAY-4 activation capability")

    _install_outbox_delivery_columns()
    _install_consumption_table()
    _install_functions()

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege("
            "'worker_runtime','finance.outbox_events','SELECT') "
            "OR pg_catalog.has_table_privilege("
            "'worker_runtime','finance.outbox_events','UPDATE') "
            "OR pg_catalog.has_table_privilege("
            "'worker_runtime','public.member_subscription_finance_event_consumptions','SELECT') "
            "OR pg_catalog.has_table_privilege("
            "'worker_runtime','public.member_subscription_finance_event_consumptions','INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("PAY-5 leaked direct event/consumption table authority to worker_runtime")


def downgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        if bind.execute(
            sa.text(
                "SELECT EXISTS("
                "SELECT 1 FROM public.member_subscription_finance_event_consumptions LIMIT 1)"
            )
        ).scalar_one():
            raise RuntimeError(
                "PAY-5 downgrade blocked: product Finance-event consumption evidence exists"
            )
        if bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM finance.outbox_events
                  WHERE aggregate_type='invoice'
                    AND event_type='finance.invoice.paid'
                    AND (
                      lease_fence<>0
                      OR leased_by IS NOT NULL
                      OR leased_until IS NOT NULL
                      OR status IN ('processing','published','failed')
                    )
                  LIMIT 1
                )
                """
            )
        ).scalar_one():
            raise RuntimeError(
                "PAY-5 downgrade blocked: Finance-event delivery evidence exists"
            )

        for signature in (_RELEASE,_ACK,_CONSUME,_CLAIM):
            op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM worker_runtime")
            op.execute(f"DROP FUNCTION {signature}")
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY pay5_consumption_security_owner_insert "
        "ON public.member_subscription_finance_event_consumptions"
    )
    op.execute(
        "DROP POLICY pay5_consumption_security_owner_select "
        "ON public.member_subscription_finance_event_consumptions"
    )
    op.execute("DROP TABLE public.member_subscription_finance_event_consumptions RESTRICT")

    op.execute(
        "REVOKE UPDATE (status,attempt_count,claimed_at,published_at,"
        "acknowledged_at,last_error_code,leased_by,leased_until,lease_fence) "
        "ON TABLE finance.outbox_events FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT (idempotency_key,payload_sha256,status,attempt_count,"
        "created_at,max_attempts,leased_by,leased_until,lease_fence) "
        "ON TABLE finance.outbox_events FROM app_security_owner"
    )

    op.execute("DROP INDEX finance.ix_finance_outbox_events_pay5_claim")
    op.execute(
        "ALTER TABLE finance.outbox_events "
        "DROP CONSTRAINT chk_finance_outbox_events_lease_pair,"
        "DROP CONSTRAINT chk_finance_outbox_events_lease_fence,"
        "DROP CONSTRAINT chk_finance_outbox_events_max_attempts"
    )
    op.execute(
        "ALTER TABLE finance.outbox_events "
        "DROP COLUMN lease_fence,"
        "DROP COLUMN leased_until,"
        "DROP COLUMN leased_by,"
        "DROP COLUMN max_attempts"
    )
