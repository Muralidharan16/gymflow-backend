"""PAY-10-C fenced refund provider execution and reconciliation capabilities.

Revision ID: zu07d8e9f0a55
Revises: zt07d8e9f0a54
Create Date: 2026-09-20

PAY-10-C exposes bounded SECURITY DEFINER capabilities over the PAY-10-A
refund provider authority/evidence schema. It does not perform provider network
I/O, post ledger entries, issue credit notes, emit financial outbox events, or
mark refunds/payments financially final. Processed provider evidence stops at
reconciliation_pending for PAY-10-D financial finalization.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zu07d8e9f0a55"
down_revision = "zt07d8e9f0a54"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_REFUND_RUNTIME = "finance_refund_runtime"
_RECON_RUNTIME = "finance_reconciliation_runtime"

_CLAIM_REFUND = (
    "app_secure.claim_pay10_refund_provider_execution(uuid,integer)"
)
_BIND_REQUEST = (
    "app_secure.bind_pay10_refund_provider_request("
    "uuid,uuid,bigint,text,text,numeric,text,text)"
)
_RECORD_OUTCOME = (
    "app_secure.record_pay10_refund_provider_outcome("
    "uuid,uuid,bigint,text,text,text,timestamp with time zone)"
)
_RECORD_UNKNOWN = (
    "app_secure.record_pay10_refund_provider_unknown(uuid,uuid,bigint,text)"
)
_RECORD_FAILURE = (
    "app_secure.record_pay10_refund_provider_failure("
    "uuid,uuid,bigint,text,boolean)"
)
_RECORD_EXTERNAL = (
    "app_secure.record_pay10_refund_external_evidence("
    "uuid,text,text,text,text,text,text,timestamp with time zone)"
)
_FUNCTIONS = (
    _CLAIM_REFUND,
    _BIND_REQUEST,
    _RECORD_OUTCOME,
    _RECORD_UNKNOWN,
    _RECORD_FAILURE,
    _RECORD_EXTERNAL,
)
_RUNTIME_ROLES = (
    "app_runtime",
    "worker_runtime",
    "finance_payment_runtime",
    _REFUND_RUNTIME,
    _RECON_RUNTIME,
    "finance_maintenance_runtime",
)


def _require_reduced_role(bind, role: str, *, login: bool = False) -> None:
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
        raise RuntimeError(f"PAY-10-C missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-10-C login posture drift: {role}")
    for key in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-10-C reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    _require_reduced_role(bind, _REFUND_RUNTIME)
    _require_reduced_role(bind, _RECON_RUNTIME)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError(
            "PAY-10-C migration requires "
            "session_user=current_user=migration_owner"
        )

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "'migration_owner','app_security_owner','SET')"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C requires bounded migration_owner SET edge "
            "to app_security_owner"
        )

    for role in _RUNTIME_ROLES:
        if bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.pg_has_role("
                    ":role,'app_security_owner','SET') "
                    "OR pg_catalog.pg_has_role("
                    ":role,'app_security_owner','MEMBER')"
                ),
                {"role": role},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"PAY-10-C runtime may reach app_security_owner: {role}"
            )


def _require_predecessor(bind) -> None:
    for relation in (
        "finance.refund_execution_commands",
        "finance.refunds",
        "finance.payments",
        "finance.payment_allocations",
        "finance.refund_provider_evidence",
    ):
        if bool(
            bind.execute(
                sa.text("SELECT pg_catalog.to_regclass(:r) IS NULL"),
                {"r": relation},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"PAY-10-C missing predecessor relation: {relation}"
            )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _REFUND_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C refuses preexisting app_secure USAGE for "
            "finance_refund_runtime"
        )
    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _RECON_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C requires inherited reconciliation app_secure USAGE"
        )
    required_acl = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_execution_commands',
                    'SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_execution_commands',
                    'UPDATE'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner','finance.payments','SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner','finance.refunds','SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner','finance.refunds','UPDATE'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.payment_allocations',
                    'SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_provider_evidence',
                    'SELECT'
                )
                AND pg_catalog.has_table_privilege(
                    'app_security_owner',
                    'finance.refund_provider_evidence',
                    'INSERT'
                )
            """
        )
    ).scalar_one()
    if not bool(required_acl):
        raise RuntimeError(
            "PAY-10-C inherited Finance owner authority is incomplete"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in _FUNCTIONS:
            if bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.to_regprocedure(:s) IS NOT NULL"
                    ),
                    {"s": signature},
                ).scalar_one()
            ):
                raise RuntimeError(
                    f"PAY-10-C predecessor already has {signature}"
                )
    finally:
        op.execute("RESET ROLE")


def _install_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            CREATE FUNCTION app_secure.claim_pay10_refund_provider_execution(
                p_worker_id uuid,
                p_limit integer DEFAULT 1
            )
            RETURNS TABLE(
                command_id uuid,
                refund_id uuid,
                payment_id uuid,
                organization_id uuid,
                provider_code text,
                provider_payment_ref text,
                amount numeric,
                currency_code char(3),
                attempt_count integer,
                lease_fence bigint,
                reclaimed_existing_attempt boolean,
                lease_expires_at timestamptz
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 claim requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-10 claim requires worker id'
                        USING ERRCODE='22023';
                END IF;

                RETURN QUERY
                WITH candidates AS (
                    SELECT
                        c.command_id,
                        c.status='processing' AS reclaiming
                    FROM finance.refund_execution_commands c
                    JOIN finance.refunds r
                      ON r.id=c.refund_id
                    WHERE r.status IN (
                        'requested','approved','processing'
                    )
                      AND (
                        (
                            c.status IN ('pending','retry_pending')
                            AND c.attempt_count<c.max_attempts
                            AND c.process_after<=pg_catalog.clock_timestamp()
                        )
                        OR (
                            c.status='processing'
                            AND c.leased_until<=pg_catalog.clock_timestamp()
                        )
                      )
                    ORDER BY
                        c.process_after,
                        c.materialized_at,
                        c.command_id
                    LIMIT greatest(
                        1,
                        least(coalesce(p_limit,1),50)
                    )
                    FOR UPDATE OF c SKIP LOCKED
                ),
                claimed AS (
                    UPDATE finance.refund_execution_commands c
                    SET
                        status='processing',
                        attempt_count=CASE
                            WHEN candidates.reclaiming
                            THEN c.attempt_count
                            ELSE c.attempt_count+1
                        END,
                        lease_fence=c.lease_fence+1,
                        leased_by=p_worker_id,
                        leased_until=pg_catalog.clock_timestamp()
                            + interval '10 minutes',
                        last_error_code=NULL,
                        updated_at=pg_catalog.clock_timestamp()
                    FROM candidates
                    WHERE c.command_id=candidates.command_id
                    RETURNING
                        c.command_id,
                        c.refund_id,
                        c.payment_id,
                        c.organization_id,
                        c.amount,
                        c.currency_code,
                        c.attempt_count,
                        c.lease_fence,
                        c.leased_until,
                        candidates.reclaiming
                )
                SELECT
                    x.command_id,
                    x.refund_id,
                    x.payment_id,
                    x.organization_id,
                    p.provider_code::text,
                    p.provider_payment_ref::text,
                    x.amount,
                    x.currency_code,
                    x.attempt_count,
                    x.lease_fence,
                    x.reclaiming,
                    x.leased_until
                FROM claimed x
                JOIN finance.payments p
                  ON p.id=x.payment_id
                ORDER BY x.command_id;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.bind_pay10_refund_provider_request(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_provider_code text,
                p_provider_payment_ref text,
                p_amount numeric,
                p_currency_code text,
                p_request_sha256 text
            )
            RETURNS TABLE(
                command_id uuid,
                refund_id uuid,
                payment_id uuid,
                organization_id uuid,
                provider_code text,
                provider_payment_ref text,
                amount numeric,
                currency_code char(3),
                request_sha256 text,
                status text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
                v_allocated numeric;
                v_reserved numeric;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 bind requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_command_id IS NULL
                   OR p_worker_id IS NULL
                   OR p_lease_fence IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 bind requires command/fence identity'
                        USING ERRCODE='22023';
                END IF;
                IF p_request_sha256 IS NULL
                   OR p_request_sha256 !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 request hash invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 refund not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_command.status<>'processing'
                   OR v_command.leased_by IS DISTINCT FROM p_worker_id
                   OR v_command.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_command.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;

                IF v_refund.payment_id IS DISTINCT FROM v_payment.id
                   OR v_command.payment_id IS DISTINCT FROM v_payment.id
                   OR v_command.refund_id IS DISTINCT FROM v_refund.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_command.legal_entity_id
                        IS DISTINCT FROM v_payment.legal_entity_id
                   OR v_refund.legal_entity_id
                        IS DISTINCT FROM v_payment.legal_entity_id
                   OR v_command.division_id
                        IS DISTINCT FROM v_payment.division_id
                   OR v_refund.division_id
                        IS DISTINCT FROM v_payment.division_id
                   OR v_command.brand_id
                        IS DISTINCT FROM v_payment.brand_id
                   OR v_refund.brand_id
                        IS DISTINCT FROM v_payment.brand_id
                   OR v_command.amount IS DISTINCT FROM v_refund.amount
                   OR v_command.currency_code
                        IS DISTINCT FROM v_payment.currency_code
                   OR v_refund.currency_code
                        IS DISTINCT FROM v_payment.currency_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10 refund authority drift'
                        USING ERRCODE='23514';
                END IF;

                IF v_refund.status NOT IN (
                    'requested','approved','processing'
                )
                   OR v_payment.status NOT IN (
                       'captured','settled','partially_refunded'
                   )
                   OR v_payment.organization_id IS NULL
                   OR nullif(
                       btrim(v_payment.provider_payment_ref),''
                   ) IS NULL
                   OR nullif(btrim(v_payment.provider_code),'') IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 refund/provider state not executable'
                        USING ERRCODE='23514';
                END IF;

                IF p_provider_code IS DISTINCT FROM v_payment.provider_code
                   OR p_provider_payment_ref
                        IS DISTINCT FROM v_payment.provider_payment_ref
                   OR p_amount IS DISTINCT FROM v_command.amount
                   OR upper(btrim(coalesce(p_currency_code,'')))
                        IS DISTINCT FROM v_command.currency_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10 caller request differs from Finance authority'
                        USING ERRCODE='23514';
                END IF;

                IF v_command.provider_refund_ref IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-10 known provider refund must reconcile, not resubmit'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.provider_code IS NOT NULL
                   AND v_command.provider_code<>v_payment.provider_code
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider binding drift'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.request_sha256 IS NOT NULL
                   AND v_command.request_sha256<>p_request_sha256
                THEN
                    RAISE EXCEPTION
                        'PAY-10 request hash drift across retry'
                        USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(a.allocated_amount),0)
                INTO v_allocated
                FROM finance.payment_allocations a
                WHERE a.payment_id=v_payment.id;

                SELECT coalesce(sum(r.amount),0)
                INTO v_reserved
                FROM finance.refunds r
                WHERE r.payment_id=v_payment.id
                  AND r.status IN (
                      'requested','approved','processing','succeeded'
                  );

                IF v_allocated<=0 OR v_reserved>v_allocated THEN
                    RAISE EXCEPTION
                        'PAY-10 refundable reservation exceeds applied value'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.refund_execution_commands c
                SET
                    request_sha256=coalesce(
                        c.request_sha256,p_request_sha256
                    ),
                    first_attempted_at=coalesce(
                        c.first_attempted_at,
                        pg_catalog.clock_timestamp()
                    ),
                    provider_code=coalesce(
                        c.provider_code,v_payment.provider_code
                    ),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id
                RETURNING c.* INTO v_command;

                UPDATE finance.refunds r
                SET
                    status='processing',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE r.id=v_refund.id
                  AND r.status IN ('requested','approved');

                RETURN QUERY SELECT
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_payment.provider_code::text,
                    v_payment.provider_payment_ref::text,
                    v_command.amount,
                    v_command.currency_code,
                    v_command.request_sha256::text,
                    v_command.status::text;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_outcome(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_provider_refund_ref text,
                p_normalized_status text,
                p_evidence_sha256 text,
                p_occurred_at timestamptz DEFAULT NULL
            )
            RETURNS TABLE(
                evidence_id uuid,
                command_id uuid,
                normalized_status text,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_existing finance.refund_provider_evidence%ROWTYPE;
                v_evidence finance.refund_provider_evidence%ROWTYPE;
                v_next text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 outcome requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_normalized_status NOT IN (
                    'pending','processed','failed'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 normalized provider status invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_evidence_sha256 IS NULL
                   OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider evidence hash invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_refund_ref IS NOT NULL
                   AND (
                       char_length(p_provider_refund_ref)>200
                       OR p_provider_refund_ref !~ '^[A-Za-z0-9_-]+$'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider refund reference invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_normalized_status<>'failed'
                   AND p_provider_refund_ref IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 accepted provider outcome requires '
                        'refund reference'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.* INTO v_existing
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=p_command_id
                  AND e.evidence_sha256=p_evidence_sha256;
                IF FOUND THEN
                    SELECT c.* INTO v_command
                    FROM finance.refund_execution_commands c
                    WHERE c.command_id=p_command_id;
                    IF v_existing.evidence_source<>'submission'
                       OR v_existing.provider_refund_ref
                            IS DISTINCT FROM p_provider_refund_ref
                       OR v_existing.normalized_status
                            IS DISTINCT FROM p_normalized_status
                       OR v_existing.occurred_at
                            IS DISTINCT FROM p_occurred_at
                    THEN
                        RAISE EXCEPTION
                            'PAY-10 conflicting provider evidence replay'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_existing.command_id,
                        v_existing.normalized_status::text,
                        v_command.status::text,
                        true;
                    RETURN;
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 outcome payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 outcome refund not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_command.status<>'processing'
                   OR v_command.leased_by IS DISTINCT FROM p_worker_id
                   OR v_command.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_command.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;

                IF v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                   OR v_command.provider_code<>v_payment.provider_code
                   OR v_command.payment_id<>v_payment.id
                   OR v_command.refund_id<>v_refund.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.payment_id<>v_payment.id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider outcome authority drift'
                        USING ERRCODE='23514';
                END IF;
                IF v_command.provider_refund_ref IS NOT NULL
                   AND v_command.provider_refund_ref
                        IS DISTINCT FROM p_provider_refund_ref
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider refund identity drift'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.refund_provider_evidence(
                    command_id,
                    refund_id,
                    payment_id,
                    organization_id,
                    provider_code,
                    provider_event_id,
                    provider_refund_ref,
                    evidence_source,
                    normalized_status,
                    request_sha256,
                    evidence_sha256,
                    occurred_at
                )
                VALUES(
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_command.provider_code,
                    NULL,
                    p_provider_refund_ref,
                    'submission',
                    p_normalized_status,
                    v_command.request_sha256,
                    p_evidence_sha256,
                    p_occurred_at
                )
                RETURNING * INTO v_evidence;

                v_next:=CASE
                    WHEN p_normalized_status='pending'
                    THEN 'provider_accepted'
                    WHEN p_normalized_status='processed'
                    THEN 'reconciliation_pending'
                    ELSE 'rejected'
                END;

                UPDATE finance.refund_execution_commands c
                SET
                    status=v_next,
                    leased_by=NULL,
                    leased_until=NULL,
                    provider_refund_ref=coalesce(
                        c.provider_refund_ref,p_provider_refund_ref
                    ),
                    provider_evidence_sha256=p_evidence_sha256,
                    provider_accepted_at=CASE
                        WHEN p_normalized_status IN ('pending','processed')
                        THEN coalesce(
                            c.provider_accepted_at,
                            pg_catalog.clock_timestamp()
                        )
                        ELSE c.provider_accepted_at
                    END,
                    last_error_code=CASE
                        WHEN p_normalized_status='failed'
                        THEN 'provider_failed'
                        ELSE NULL
                    END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN QUERY SELECT
                    v_evidence.id,
                    v_evidence.command_id,
                    v_evidence.normalized_status::text,
                    v_next,
                    false;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_unknown(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_error_code text
            )
            RETURNS text
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_error text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_error:=coalesce(
                    nullif(btrim(p_error_code),''),
                    'provider_unknown'
                );
                IF v_error !~ '^[a-z][a-z0-9_]{0,63}$'
                   OR v_error ~ '(bearer|secret|token)'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown error code invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                  AND c.status='processing'
                  AND c.leased_by=p_worker_id
                  AND c.lease_fence=p_lease_fence
                  AND c.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;
                IF v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 unknown outcome requires bound request'
                        USING ERRCODE='23514';
                END IF;

                UPDATE finance.refund_execution_commands c
                SET
                    status='reconciliation_pending',
                    leased_by=NULL,
                    leased_until=NULL,
                    last_error_code=v_error,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN 'reconciliation_pending';
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_provider_failure(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint,
                p_error_code text,
                p_permanent boolean DEFAULT false
            )
            RETURNS text
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_error text;
                v_next text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_refund_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 failure requires finance_refund_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_error:=coalesce(
                    nullif(btrim(p_error_code),''),
                    'provider_failure'
                );
                IF v_error !~ '^[a-z][a-z0-9_]{0,63}$'
                   OR v_error ~ '(bearer|secret|token)'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 failure error code invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                  AND c.status='processing'
                  AND c.leased_by=p_worker_id
                  AND c.lease_fence=p_lease_fence
                  AND c.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 stale refund execution fence'
                        USING ERRCODE='40001';
                END IF;

                v_next:=CASE
                    WHEN coalesce(p_permanent,false)
                         OR v_command.attempt_count>=v_command.max_attempts
                    THEN 'dead_lettered'
                    ELSE 'retry_pending'
                END;

                UPDATE finance.refund_execution_commands c
                SET
                    status=v_next,
                    leased_by=NULL,
                    leased_until=NULL,
                    process_after=CASE
                        WHEN v_next='retry_pending'
                        THEN pg_catalog.clock_timestamp()+(
                            least(
                                1800,
                                30 * (
                                    2 ^ greatest(
                                        v_command.attempt_count-1,
                                        0
                                    )
                                )
                            ) * interval '1 second'
                        )
                        ELSE c.process_after
                    END,
                    last_error_code=v_error,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN v_next;
            END
            $function$
            """
        )

        op.execute(
            """
            CREATE FUNCTION app_secure.record_pay10_refund_external_evidence(
                p_command_id uuid,
                p_provider_payment_ref text,
                p_provider_event_id text,
                p_provider_refund_ref text,
                p_evidence_source text,
                p_normalized_status text,
                p_evidence_sha256 text,
                p_occurred_at timestamptz DEFAULT NULL
            )
            RETURNS TABLE(
                evidence_id uuid,
                command_id uuid,
                normalized_status text,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_command finance.refund_execution_commands%ROWTYPE;
                v_payment finance.payments%ROWTYPE;
                v_refund finance.refunds%ROWTYPE;
                v_existing finance.refund_provider_evidence%ROWTYPE;
                v_evidence finance.refund_provider_evidence%ROWTYPE;
                v_has_processed boolean;
                v_next text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'finance_reconciliation_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence requires '
                        'finance_reconciliation_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_evidence_source NOT IN (
                    'webhook','reconciliation'
                )
                   OR p_normalized_status NOT IN (
                       'pending','processed','failed'
                   )
                   OR p_evidence_sha256 IS NULL
                   OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence shape invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_evidence_source='webhook'
                   AND (
                       p_provider_event_id IS NULL
                       OR btrim(p_provider_event_id)=''
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 webhook evidence requires event identity'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_event_id IS NOT NULL
                   AND (
                       char_length(p_provider_event_id)>200
                       OR p_provider_event_id !~ '^[A-Za-z0-9_.:-]+$'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 provider event identity invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_provider_refund_ref IS NOT NULL
                   AND (
                       char_length(p_provider_refund_ref)>200
                       OR p_provider_refund_ref !~ '^[A-Za-z0-9_-]+$'
                   )
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external refund reference invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_normalized_status<>'failed'
                   AND p_provider_refund_ref IS NULL
                THEN
                    RAISE EXCEPTION
                        'PAY-10 accepted external evidence requires '
                        'refund reference'
                        USING ERRCODE='22023';
                END IF;

                SELECT p.* INTO v_payment
                FROM finance.payments p
                JOIN finance.refund_execution_commands c
                  ON c.payment_id=p.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 external payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT r.* INTO v_refund
                FROM finance.refunds r
                JOIN finance.refund_execution_commands c
                  ON c.refund_id=r.id
                WHERE c.command_id=p_command_id
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-10 external refund not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT c.* INTO v_command
                FROM finance.refund_execution_commands c
                WHERE c.command_id=p_command_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_command.request_sha256 IS NULL
                   OR v_command.provider_code IS NULL
                   OR v_command.provider_code<>v_payment.provider_code
                   OR v_payment.provider_payment_ref
                        IS DISTINCT FROM p_provider_payment_ref
                   OR v_command.payment_id<>v_payment.id
                   OR v_command.refund_id<>v_refund.id
                   OR v_command.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                   OR v_refund.organization_id
                        IS DISTINCT FROM v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external evidence authority mismatch'
                        USING ERRCODE='23514';
                END IF;

                IF v_command.status='processing'
                   AND v_command.leased_until>pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-10 active refund lease blocks reconciliation'
                        USING ERRCODE='40001';
                END IF;
                IF v_command.status NOT IN (
                    'processing',
                    'provider_accepted',
                    'reconciliation_pending',
                    'rejected',
                    'dead_lettered',
                    'succeeded'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-10 command is not reconcilable'
                        USING ERRCODE='23514';
                END IF;

                IF v_command.provider_refund_ref IS NOT NULL
                   AND p_provider_refund_ref IS NOT NULL
                   AND v_command.provider_refund_ref<>p_provider_refund_ref
                THEN
                    RAISE EXCEPTION
                        'PAY-10 external provider refund identity drift'
                        USING ERRCODE='23514';
                END IF;

                IF p_provider_event_id IS NOT NULL THEN
                    SELECT e.* INTO v_existing
                    FROM finance.refund_provider_evidence e
                    WHERE e.provider_code=v_command.provider_code
                      AND e.provider_event_id=p_provider_event_id;
                    IF FOUND THEN
                        IF v_existing.command_id<>v_command.command_id
                           OR v_existing.provider_refund_ref
                                IS DISTINCT FROM p_provider_refund_ref
                           OR v_existing.evidence_source
                                <>p_evidence_source
                           OR v_existing.normalized_status
                                <>p_normalized_status
                           OR v_existing.evidence_sha256
                                <>p_evidence_sha256
                           OR v_existing.occurred_at
                                IS DISTINCT FROM p_occurred_at
                        THEN
                            RAISE EXCEPTION
                                'PAY-10 conflicting provider event replay'
                                USING ERRCODE='23505';
                        END IF;
                        RETURN QUERY SELECT
                            v_existing.id,
                            v_existing.command_id,
                            v_existing.normalized_status::text,
                            v_command.status::text,
                            true;
                        RETURN;
                    END IF;
                END IF;

                SELECT e.* INTO v_existing
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=v_command.command_id
                  AND e.evidence_sha256=p_evidence_sha256;
                IF FOUND THEN
                    IF v_existing.provider_event_id
                            IS DISTINCT FROM p_provider_event_id
                       OR v_existing.provider_refund_ref
                            IS DISTINCT FROM p_provider_refund_ref
                       OR v_existing.evidence_source<>p_evidence_source
                       OR v_existing.normalized_status<>p_normalized_status
                       OR v_existing.occurred_at
                            IS DISTINCT FROM p_occurred_at
                    THEN
                        RAISE EXCEPTION
                            'PAY-10 conflicting external evidence replay'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.id,
                        v_existing.command_id,
                        v_existing.normalized_status::text,
                        v_command.status::text,
                        true;
                    RETURN;
                END IF;

                INSERT INTO finance.refund_provider_evidence(
                    command_id,
                    refund_id,
                    payment_id,
                    organization_id,
                    provider_code,
                    provider_event_id,
                    provider_refund_ref,
                    evidence_source,
                    normalized_status,
                    request_sha256,
                    evidence_sha256,
                    occurred_at
                )
                VALUES(
                    v_command.command_id,
                    v_command.refund_id,
                    v_command.payment_id,
                    v_command.organization_id,
                    v_command.provider_code,
                    p_provider_event_id,
                    p_provider_refund_ref,
                    p_evidence_source,
                    p_normalized_status,
                    v_command.request_sha256,
                    p_evidence_sha256,
                    p_occurred_at
                )
                RETURNING * INTO v_evidence;

                SELECT EXISTS(
                    SELECT 1
                    FROM finance.refund_provider_evidence e
                    WHERE e.command_id=v_command.command_id
                      AND e.normalized_status='processed'
                )
                INTO v_has_processed;

                v_next:=CASE
                    WHEN v_command.status='succeeded'
                    THEN 'succeeded'
                    WHEN v_has_processed
                    THEN 'reconciliation_pending'
                    WHEN p_normalized_status='failed'
                    THEN 'rejected'
                    WHEN v_command.status='reconciliation_pending'
                    THEN 'reconciliation_pending'
                    ELSE 'provider_accepted'
                END;

                UPDATE finance.refund_execution_commands c
                SET
                    status=v_next,
                    leased_by=NULL,
                    leased_until=NULL,
                    provider_refund_ref=coalesce(
                        c.provider_refund_ref,p_provider_refund_ref
                    ),
                    provider_evidence_sha256=CASE
                        WHEN p_normalized_status='processed'
                             OR c.provider_evidence_sha256 IS NULL
                        THEN p_evidence_sha256
                        ELSE c.provider_evidence_sha256
                    END,
                    provider_accepted_at=CASE
                        WHEN p_normalized_status IN ('pending','processed')
                        THEN coalesce(
                            c.provider_accepted_at,
                            pg_catalog.clock_timestamp()
                        )
                        ELSE c.provider_accepted_at
                    END,
                    last_error_code=CASE
                        WHEN v_next='rejected'
                        THEN 'provider_failed'
                        WHEN v_next='succeeded'
                        THEN c.last_error_code
                        ELSE NULL
                    END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id;

                RETURN QUERY SELECT
                    v_evidence.id,
                    v_evidence.command_id,
                    v_evidence.normalized_status::text,
                    v_next,
                    false;
            END
            $function$
            """
        )

        for signature in _FUNCTIONS:
            op.execute(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"
            )

        op.execute(
            "GRANT USAGE ON SCHEMA app_secure TO finance_refund_runtime"
        )
        for signature in (
            _CLAIM_REFUND,
            _BIND_REQUEST,
            _RECORD_OUTCOME,
            _RECORD_UNKNOWN,
            _RECORD_FAILURE,
        ):
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} "
                "TO finance_refund_runtime"
            )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_RECORD_EXTERNAL} "
            "TO finance_reconciliation_runtime"
        )
    finally:
        op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    # migration_owner is deliberately app_secure-blind. Function catalog
    # resolution and ACL proofs therefore execute only under the bounded
    # NOLOGIN security-owner context and are reset before migration exit.
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        expected = {
            _CLAIM_REFUND: {_REFUND_RUNTIME},
            _BIND_REQUEST: {_REFUND_RUNTIME},
            _RECORD_OUTCOME: {_REFUND_RUNTIME},
            _RECORD_UNKNOWN: {_REFUND_RUNTIME},
            _RECORD_FAILURE: {_REFUND_RUNTIME},
            _RECORD_EXTERNAL: {_RECON_RUNTIME},
        }
        for signature, allowed in expected.items():
            row = bind.execute(
                sa.text(
                    """
                    SELECT
                        pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                        p.prosecdef,
                        coalesce(array_to_string(p.proconfig,','),'') AS config,
                        EXISTS (
                            SELECT 1
                            FROM pg_catalog.aclexplode(
                                coalesce(
                                    p.proacl,
                                    pg_catalog.acldefault('f',p.proowner)
                                )
                            ) acl
                            WHERE acl.grantee=0
                              AND acl.privilege_type='EXECUTE'
                        ) AS public_execute
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid=pg_catalog.to_regprocedure(:signature)
                    """
                ),
                {"signature": signature},
            ).mappings().one_or_none()
            if (
                row is None
                or row["owner"] != _SECURITY_OWNER
                or not bool(row["prosecdef"])
                or bool(row["public_execute"])
                or "row_security=on" not in row["config"]
            ):
                raise RuntimeError(
                    f"PAY-10-C function security drift: {signature}"
                )
    
            for role in _RUNTIME_ROLES:
                actual = bool(
                    bind.execute(
                        sa.text(
                            "SELECT pg_catalog.has_function_privilege("
                            ":role,:signature,'EXECUTE')"
                        ),
                        {"role": role, "signature": signature},
                    ).scalar_one()
                )
                if actual != (role in allowed):
                    raise RuntimeError(
                        "PAY-10-C execute ACL drift: "
                        f"{role} -> {signature}"
                    )
    
        for role in (_REFUND_RUNTIME, _RECON_RUNTIME):
            for relation in (
                "finance.refund_execution_commands",
                "finance.refunds",
                "finance.payments",
                "finance.payment_allocations",
                "finance.refund_provider_evidence",
            ):
                direct = bool(
                    bind.execute(
                        sa.text(
                            """
                            SELECT
                                pg_catalog.has_table_privilege(
                                    :role,:relation,'SELECT'
                                )
                                OR pg_catalog.has_table_privilege(
                                    :role,:relation,'INSERT'
                                )
                                OR pg_catalog.has_table_privilege(
                                    :role,:relation,'UPDATE'
                                )
                                OR pg_catalog.has_table_privilege(
                                    :role,:relation,'DELETE'
                                )
                            """
                        ),
                        {"role": role, "relation": relation},
                    ).scalar_one()
                )
                if direct:
                    raise RuntimeError(
                        "PAY-10-C direct Finance DML leaked: "
                        f"{role} -> {relation}"
                    )
    
    
    finally:
        op.execute("RESET ROLE")

def _has_execution_evidence(bind) -> bool:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        count = bind.execute(
            sa.text(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM finance.refund_provider_evidence
                    )
                    +
                    (
                        SELECT count(*)
                        FROM finance.refund_execution_commands
                        WHERE request_sha256 IS NOT NULL
                           OR first_attempted_at IS NOT NULL
                           OR provider_accepted_at IS NOT NULL
                           OR provider_evidence_sha256 IS NOT NULL
                           OR provider_refund_ref IS NOT NULL
                           OR status IN (
                               'provider_accepted',
                               'reconciliation_pending',
                               'succeeded',
                               'rejected'
                           )
                    )
                """
            )
        ).scalar_one()
    finally:
        op.execute("RESET ROLE")
    return int(count) != 0


def _drop_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _CLAIM_REFUND,
            _BIND_REQUEST,
            _RECORD_OUTCOME,
            _RECORD_UNKNOWN,
            _RECORD_FAILURE,
        ):
            op.execute(
                f"REVOKE EXECUTE ON FUNCTION {signature} "
                "FROM finance_refund_runtime"
            )
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_RECORD_EXTERNAL} "
            "FROM finance_reconciliation_runtime"
        )
        op.execute(
            "REVOKE USAGE ON SCHEMA app_secure "
            "FROM finance_refund_runtime"
        )
        for signature in reversed(_FUNCTIONS):
            op.execute(f"DROP FUNCTION {signature}")
    finally:
        op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    _require_predecessor(bind)
    _install_functions()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    if _has_execution_evidence(bind):
        raise RuntimeError(
            "PAY-10-C downgrade blocked: durable refund provider "
            "execution/reconciliation evidence exists"
        )

    _drop_functions()

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        removed = bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.to_regprocedure(:s) IS NULL"
                ),
                {"s": _CLAIM_REFUND},
            ).scalar_one()
        )
    finally:
        op.execute("RESET ROLE")
    if not removed:
        raise RuntimeError(
            "PAY-10-C downgrade failed to remove capability functions"
        )
    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _REFUND_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY-10-C downgrade failed to restore refund runtime posture"
        )
