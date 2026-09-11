"""Create P4D refund authority command boundary without provider execution.

Revision ID: zc07d8e9f0a3d
Revises: zb07d8e9f0a3c
Create Date: 2026-08-18

P4D-1 deliberately separates Finance refund intent from provider execution.
``finance.refunds`` remains the business intent row.  The new
``finance.refund_execution_commands`` relation owns future execution leasing
and fencing.  No provider call, provider acknowledgement, provider terminal
success, or lifecycle refund delivery semantics are introduced here.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zc07d8e9f0a3d"
down_revision = "zb07d8e9f0a3c"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER_ROLE = "worker_runtime"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_APP_RUNTIME_ROLE = "app_runtime"
_AUTH_RUNTIME_ROLE = "auth_runtime"
_REFUNDS = "finance.refunds"
_PAYMENTS = "finance.payments"
_COMMANDS = "finance.refund_execution_commands"
_COMMAND_POLICY = "p4d_refund_execution_security_owner_all"
_FUNCTIONS = (
    "app_secure.materialize_refund_execution_command(uuid,text,uuid,text)",
    "app_secure.claim_refund_execution_command(uuid,integer)",
    "app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean)",
    "app_secure.discover_refund_execution_maintenance(integer)",
)
_RUNTIME_ROLES = (
    _APP_RUNTIME_ROLE,
    _AUTH_RUNTIME_ROLE,
    _WORKER_ROLE,
    _MAINTENANCE_ROLE,
)


def _require_reduced_role(bind, role_name: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=:role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()
    if row is None or any(bool(row[key]) for key in row):
        raise RuntimeError(f"zc07 reduced-role contract drift: {role_name}")


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,current_user::text AS current_name,
                   rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("zc07 P4D migration requires migration_owner")
    if any(bool(row[key]) for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
    )):
        raise RuntimeError("zc07 migration_owner violates reduced-role contract")

    _require_reduced_role(bind, _SECURITY_OWNER)
    for role_name in _RUNTIME_ROLES:
        _require_reduced_role(bind, role_name)
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": role_name, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"zc07 runtime may SET ROLE app_security_owner: {role_name}")


def _require_predecessor(bind) -> None:
    for relation in (_REFUNDS, _PAYMENTS):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"zc07 missing predecessor relation {relation}")
    if bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute "
            "WHERE attrelid='finance.refunds'::regclass AND attname='currency_code' AND NOT attisdropped)"
        )
    ).scalar_one():
        raise RuntimeError("zc07 refuses ambiguous predecessor finance.refunds.currency_code")
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"),
        {"relation": _COMMANDS},
    ).scalar_one():
        raise RuntimeError("zc07 refund execution command relation already exists")
    for role_name in (_WORKER_ROLE, _MAINTENANCE_ROLE):
        if not bind.execute(
            sa.text("SELECT pg_catalog.has_schema_privilege(:role_name, 'app_secure', 'USAGE')"),
            {"role_name": role_name},
        ).scalar_one():
            raise RuntimeError(f"zc07 requires existing app_secure USAGE for {role_name}")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role('migration_owner','app_security_owner','SET')")
    ).scalar_one():
        raise RuntimeError("zc07 requires migration_owner SET edge to app_security_owner")


def _install_schema() -> None:
    op.execute("ALTER TABLE finance.refunds ADD COLUMN currency_code CHAR(3)")
    op.execute(
        """
        UPDATE finance.refunds AS refund_data
        SET currency_code = payment_data.currency_code
        FROM finance.payments AS payment_data
        WHERE payment_data.id = refund_data.payment_id
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM finance.refunds WHERE currency_code IS NULL) THEN
                RAISE EXCEPTION 'zc07 cannot derive currency_code for every existing refund';
            END IF;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE finance.refunds ALTER COLUMN currency_code SET NOT NULL")
    op.execute(
        """
        ALTER TABLE finance.refunds
            ADD CONSTRAINT chk_finance_refunds_currency
            CHECK (currency_code ~ '^[A-Z]{3}$')
        """
    )
    op.execute(
        """
        ALTER TABLE finance.payments
            ADD CONSTRAINT uq_finance_payments_id_currency
            UNIQUE (id, currency_code)
        """
    )
    op.execute(
        """
        ALTER TABLE finance.refunds
            ADD CONSTRAINT fk_finance_refunds_payment_currency
            FOREIGN KEY (payment_id, currency_code)
            REFERENCES finance.payments(id, currency_code)
            ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_finance_refunds_payment_reason_not_null
            ON finance.refunds(payment_id, reason_code)
            WHERE reason_code IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE finance.refund_execution_commands (
            command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            refund_id UUID NOT NULL REFERENCES finance.refunds(id) ON DELETE RESTRICT,
            payment_id UUID NOT NULL REFERENCES finance.payments(id) ON DELETE RESTRICT,
            organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            division_id UUID NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            source_type VARCHAR(80) NOT NULL,
            source_id UUID NOT NULL,
            logical_obligation_key TEXT NOT NULL,
            amount NUMERIC(14,2) NOT NULL,
            currency_code CHAR(3) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 10,
            lease_fence BIGINT NOT NULL DEFAULT 0,
            process_after TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            leased_by UUID NULL,
            leased_until TIMESTAMPTZ NULL,
            last_error_code VARCHAR(64) NULL,
            materialized_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            provider_code VARCHAR(40) NULL,
            provider_refund_ref VARCHAR(200) NULL,
            provider_evidence_sha256 CHAR(64) NULL,
            CONSTRAINT uq_finance_refund_execution_refund UNIQUE (refund_id),
            CONSTRAINT uq_finance_refund_execution_logical_key UNIQUE (logical_obligation_key),
            CONSTRAINT chk_finance_refund_execution_source CHECK (btrim(source_type) <> ''),
            CONSTRAINT chk_finance_refund_execution_amount CHECK (amount > 0),
            CONSTRAINT chk_finance_refund_execution_currency CHECK (currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_finance_refund_execution_status CHECK (
                status IN ('pending','processing','retry_pending','provider_accepted','reconciliation_pending','succeeded','rejected','dead_lettered','cancelled')
            ),
            CONSTRAINT chk_finance_refund_execution_attempts CHECK (
                max_attempts BETWEEN 1 AND 20 AND attempt_count BETWEEN 0 AND max_attempts
            ),
            CONSTRAINT chk_finance_refund_execution_lease_fence CHECK (lease_fence >= 0),
            CONSTRAINT chk_finance_refund_execution_error_code CHECK (
                last_error_code IS NULL OR (last_error_code ~ '^[a-z][a-z0-9_]{0,63}$' AND last_error_code !~ '(bearer|secret|token)')
            ),
            CONSTRAINT chk_finance_refund_execution_lease CHECK (
                (status = 'processing') = (leased_by IS NOT NULL AND leased_until IS NOT NULL)
            ),
            CONSTRAINT chk_finance_refund_execution_provider_evidence CHECK (
                provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_refund_execution_claimable
            ON finance.refund_execution_commands(process_after, materialized_at, command_id)
            WHERE status IN ('pending','retry_pending')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_refund_execution_processing
            ON finance.refund_execution_commands(leased_until, command_id)
            WHERE status = 'processing'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_finance_refund_execution_maintenance
            ON finance.refund_execution_commands(status, process_after, updated_at)
        """
    )
    op.execute("GRANT USAGE ON SCHEMA finance TO app_security_owner")
    op.execute("GRANT SELECT, UPDATE ON TABLE finance.payments TO app_security_owner")
    op.execute("GRANT SELECT, UPDATE ON TABLE finance.refunds TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE public.branch_outbox_events TO app_security_owner")
    op.execute("ALTER TABLE finance.refund_execution_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.refund_execution_commands FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.refund_execution_commands FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE finance.refund_execution_commands TO app_security_owner")
    op.execute(
        """
        CREATE POLICY p4d_refund_execution_security_owner_all
        ON finance.refund_execution_commands
        FOR ALL
        TO app_security_owner
        USING (true)
        WITH CHECK (true)
        """
    )


def _install_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        """
        CREATE FUNCTION app_secure.materialize_refund_execution_command(
            p_refund_id uuid,
            p_source_type text,
            p_source_id uuid,
            p_idempotency_key text
        )
        RETURNS TABLE(
            command_id uuid,
            refund_id uuid,
            payment_id uuid,
            organization_id uuid,
            amount numeric,
            currency_code char(3),
            lease_fence bigint,
            status text,
            reused boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_refund finance.refunds%ROWTYPE;
            v_payment finance.payments%ROWTYPE;
            v_source public.branch_outbox_events%ROWTYPE;
            v_existing finance.refund_execution_commands%ROWTYPE;
            v_key text;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'worker_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D refund materialization requires worker_runtime' USING ERRCODE='42501';
            END IF;
            IF p_refund_id IS NULL OR p_source_id IS NULL OR btrim(coalesce(p_source_type,'')) = '' THEN
                RAISE EXCEPTION 'P4D refund materialization requires refund/source identity' USING ERRCODE='22023';
            END IF;
            IF btrim(coalesce(p_idempotency_key,'')) = '' THEN
                RAISE EXCEPTION 'P4D refund materialization requires idempotency key' USING ERRCODE='22023';
            END IF;

            SELECT r.* INTO v_refund
            FROM finance.refunds r
            WHERE r.id = p_refund_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund intent not found' USING ERRCODE='P0002';
            END IF;

            SELECT p.* INTO v_payment
            FROM finance.payments p
            WHERE p.id = v_refund.payment_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund payment not found' USING ERRCODE='P0002';
            END IF;

            SELECT r.* INTO v_refund
            FROM finance.refunds r
            WHERE r.id = p_refund_id
              AND r.payment_id = v_payment.id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund/payment lock drift' USING ERRCODE='40001';
            END IF;
            IF v_refund.status NOT IN ('requested','approved','processing') THEN
                RAISE EXCEPTION 'P4D refund status is not execution-eligible' USING ERRCODE='23514';
            END IF;
            IF v_refund.amount <= 0 THEN
                RAISE EXCEPTION 'P4D refund amount must be positive' USING ERRCODE='23514';
            END IF;
            IF v_refund.organization_id IS DISTINCT FROM v_payment.organization_id
               OR v_refund.legal_entity_id IS DISTINCT FROM v_payment.legal_entity_id
               OR v_refund.division_id IS DISTINCT FROM v_payment.division_id
               OR v_refund.brand_id IS DISTINCT FROM v_payment.brand_id
               OR v_refund.currency_code IS DISTINCT FROM v_payment.currency_code THEN
                RAISE EXCEPTION 'P4D refund/payment authority mismatch' USING ERRCODE='23514';
            END IF;
            IF v_payment.organization_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund execution requires tenant-bound payment' USING ERRCODE='23514';
            END IF;
            IF btrim(p_source_type) <> 'branch.refund_required' THEN
                RAISE EXCEPTION 'P4D refund source type is not supported' USING ERRCODE='23514';
            END IF;
            SELECT o.* INTO v_source
            FROM public.branch_outbox_events o
            WHERE o.outbox_id = p_source_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund source outbox event not found' USING ERRCODE='P0002';
            END IF;
            IF v_source.event_type <> 'branch.refund_required'
               OR v_source.tenant_id IS DISTINCT FROM v_payment.organization_id THEN
                RAISE EXCEPTION 'P4D refund source tenant mismatch' USING ERRCODE='23514';
            END IF;

            v_key := 'finance-refund/' || v_refund.id::text;
            SELECT c.* INTO v_existing
            FROM finance.refund_execution_commands c
            WHERE c.refund_id = v_refund.id
            FOR UPDATE;
            IF FOUND THEN
                IF v_existing.logical_obligation_key <> v_key
                   OR v_existing.amount <> v_refund.amount
                   OR v_existing.currency_code <> v_payment.currency_code
                   OR v_existing.organization_id <> v_payment.organization_id THEN
                    RAISE EXCEPTION 'P4D existing refund command authority drift' USING ERRCODE='23514';
                END IF;
                RETURN QUERY SELECT v_existing.command_id,v_existing.refund_id,v_existing.payment_id,
                    v_existing.organization_id,v_existing.amount,v_existing.currency_code,
                    v_existing.lease_fence,v_existing.status,true;
                RETURN;
            END IF;

            INSERT INTO finance.refund_execution_commands(
                refund_id,payment_id,organization_id,legal_entity_id,division_id,brand_id,
                source_type,source_id,logical_obligation_key,amount,currency_code,status
            )
            VALUES (
                v_refund.id,v_payment.id,v_payment.organization_id,v_payment.legal_entity_id,
                v_payment.division_id,v_payment.brand_id,btrim(p_source_type),p_source_id,
                v_key,v_refund.amount,v_payment.currency_code,'pending'
            )
            RETURNING * INTO v_existing;

            RETURN QUERY SELECT v_existing.command_id,v_existing.refund_id,v_existing.payment_id,
                v_existing.organization_id,v_existing.amount,v_existing.currency_code,
                v_existing.lease_fence,v_existing.status,false;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.claim_refund_execution_command(
            p_worker_id uuid,
            p_limit integer DEFAULT 1
        )
        RETURNS TABLE(
            command_id uuid,
            refund_id uuid,
            payment_id uuid,
            organization_id uuid,
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
        AS $$
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'worker_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D refund claim requires worker_runtime' USING ERRCODE='42501';
            END IF;
            IF p_worker_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund claim requires worker id' USING ERRCODE='22023';
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT c.command_id, c.status = 'processing' AS reclaiming
                FROM finance.refund_execution_commands c
                JOIN finance.refunds r ON r.id = c.refund_id
                WHERE r.status IN ('requested','approved','processing')
                  AND (
                    (c.status IN ('pending','retry_pending') AND c.attempt_count < c.max_attempts AND c.process_after <= pg_catalog.clock_timestamp())
                    OR (c.status = 'processing' AND c.leased_until <= pg_catalog.clock_timestamp())
                  )
                ORDER BY c.process_after, c.materialized_at, c.command_id
                LIMIT greatest(1, least(coalesce(p_limit,1), 50))
                FOR UPDATE SKIP LOCKED
            )
            UPDATE finance.refund_execution_commands AS c
            SET status='processing',
                attempt_count=CASE WHEN candidates.reclaiming THEN c.attempt_count ELSE c.attempt_count + 1 END,
                lease_fence=c.lease_fence + 1,
                leased_by=p_worker_id,
                leased_until=pg_catalog.clock_timestamp() + interval '10 minutes',
                last_error_code=NULL,
                updated_at=pg_catalog.clock_timestamp()
            FROM candidates
            WHERE c.command_id = candidates.command_id
            RETURNING c.command_id,c.refund_id,c.payment_id,c.organization_id,
                c.amount,c.currency_code,c.attempt_count,c.lease_fence,candidates.reclaiming,c.leased_until;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.record_refund_execution_failure(
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
        AS $$
        DECLARE
            v_command finance.refund_execution_commands%ROWTYPE;
            v_next_status text;
            v_error_code text;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'worker_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D refund failure recording requires worker_runtime' USING ERRCODE='42501';
            END IF;
            IF p_lease_fence IS NULL THEN
                RAISE EXCEPTION 'P4D refund failure recording requires lease fence' USING ERRCODE='22023';
            END IF;
            v_error_code := coalesce(nullif(btrim(p_error_code),''),'unknown_error');
            IF v_error_code !~ '^[a-z][a-z0-9_]{0,63}$' OR v_error_code ~ '(bearer|secret|token)' THEN
                RAISE EXCEPTION 'P4D refund failure error code must be a bounded machine token' USING ERRCODE='23514';
            END IF;
            SELECT c.* INTO v_command
            FROM finance.refund_execution_commands c
            WHERE c.command_id=p_command_id
              AND c.status='processing'
              AND c.leased_by=p_worker_id
              AND c.lease_fence=p_lease_fence
              AND c.leased_until > pg_catalog.clock_timestamp()
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D stale refund execution fence' USING ERRCODE='40001';
            END IF;
            IF coalesce(p_permanent,false) OR v_command.attempt_count >= v_command.max_attempts THEN
                v_next_status := 'dead_lettered';
            ELSE
                v_next_status := 'retry_pending';
            END IF;
            UPDATE finance.refund_execution_commands
            SET status=v_next_status,
                leased_by=NULL,
                leased_until=NULL,
                process_after=CASE WHEN v_next_status='retry_pending'
                    THEN pg_catalog.clock_timestamp() + (
                        least(1800, 30 * (2 ^ greatest(v_command.attempt_count - 1, 0))) * interval '1 second'
                    )
                    ELSE process_after END,
                last_error_code=v_error_code,
                updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=v_command.command_id;
            RETURN v_next_status;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.discover_refund_execution_maintenance(
            p_limit integer DEFAULT 100
        )
        RETURNS TABLE(
            command_id uuid,
            organization_id uuid,
            status text,
            attempt_count integer,
            process_after timestamptz,
            leased_until timestamptz,
            last_error_code text
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
            SELECT c.command_id,c.organization_id,c.status,c.attempt_count,
                   c.process_after,c.leased_until,c.last_error_code::text
            FROM finance.refund_execution_commands c
            JOIN finance.refunds r ON r.id = c.refund_id
            WHERE r.status IN ('requested','approved','processing')
              AND (c.status IN ('retry_pending','processing','provider_accepted','reconciliation_pending','dead_lettered')
                   OR c.process_after <= pg_catalog.clock_timestamp())
            ORDER BY c.updated_at, c.command_id
            LIMIT greatest(1, least(coalesce(p_limit,100), 500))
        $$;
        """
    )
    for signature in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.materialize_refund_execution_command(uuid,text,uuid,text) TO worker_runtime"
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.claim_refund_execution_command(uuid,integer) TO worker_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean) TO worker_runtime")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.discover_refund_execution_maintenance(integer) TO lifecycle_maintenance_runtime"
    )
    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    enabled, forced = bind.execute(
        sa.text(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE oid='finance.refund_execution_commands'::regclass"
        )
    ).one()
    if not enabled or not forced:
        raise RuntimeError("zc07 refund execution commands must retain ENABLE+FORCE RLS")
    policy_row = bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   ARRAY(
                       SELECT r.rolname
                       FROM pg_catalog.pg_roles r
                       WHERE r.oid = ANY(p.polroles)
                       ORDER BY r.rolname
                   ) AS roles
            FROM pg_catalog.pg_policy p
            WHERE p.polrelid='finance.refund_execution_commands'::regclass
              AND p.polname=:policy_name
            """
        ),
        {"policy_name": _COMMAND_POLICY},
    ).mappings().one_or_none()
    if policy_row is None or policy_row["command"] != "*" or list(policy_row["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zc07 refund execution command policy role/command drift")
    for role_name in _RUNTIME_ROLES:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role,'finance.refund_execution_commands','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.refund_execution_commands','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.refund_execution_commands','UPDATE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(f"zc07 direct refund command table privilege leaked to {role_name}")
    for signature in _FUNCTIONS:
        function_name = signature.split("(", 1)[0].rsplit(".", 1)[1]
        function_args = signature.split("(", 1)[1].rstrip(")").replace(" ", "")
        row = bind.execute(
            sa.text(
                """
                SELECT pg_get_userbyid(p.proowner) AS owner, p.prosecdef,
                       coalesce(p.proconfig::text,'') AS config,
                       EXISTS (
                           SELECT 1
                           FROM pg_catalog.aclexplode(coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))) acl
                           WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                       ) AS public_execute
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname=:function_name
                  AND replace(pg_catalog.oidvectortypes(p.proargtypes), ' ', '')=:function_args
                """
            ),
            {"function_name": function_name, "function_args": function_args},
        ).mappings().one_or_none()
        if row is None or row["owner"] != _SECURITY_OWNER or not row["prosecdef"]:
            raise RuntimeError(f"zc07 function owner/security drift: {signature}")
        if "search_path=pg_catalog, public, finance" not in row["config"] and "search_path=pg_catalog,public,finance" not in row["config"]:
            raise RuntimeError(f"zc07 function search_path drift: {signature}")
        if row["public_execute"]:
            raise RuntimeError(f"zc07 PUBLIC execute leaked: {signature}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)
    _install_schema()
    _install_functions()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('finance.refund_execution_commands') IS NOT NULL")
    ).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_refund_execution_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.refund_execution_commands LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_refund_execution_evidence:
            raise RuntimeError(
                "zc07 downgrade blocked: refund execution authority/evidence exists"
            )
    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("RESET ROLE")
    op.execute("DROP POLICY IF EXISTS p4d_refund_execution_security_owner_all ON finance.refund_execution_commands")
    op.execute("DROP TABLE IF EXISTS finance.refund_execution_commands RESTRICT")
    op.execute("REVOKE SELECT ON TABLE public.branch_outbox_events FROM app_security_owner")
    op.execute("REVOKE SELECT, UPDATE ON TABLE finance.refunds FROM app_security_owner")
    op.execute("REVOKE SELECT, UPDATE ON TABLE finance.payments FROM app_security_owner")
    op.execute("REVOKE USAGE ON SCHEMA finance FROM app_security_owner")
    op.execute("DROP INDEX IF EXISTS finance.uq_finance_refunds_payment_reason_not_null")
    op.execute("ALTER TABLE finance.refunds DROP CONSTRAINT IF EXISTS fk_finance_refunds_payment_currency")
    op.execute("ALTER TABLE finance.payments DROP CONSTRAINT IF EXISTS uq_finance_payments_id_currency")
    op.execute("ALTER TABLE finance.refunds DROP CONSTRAINT IF EXISTS chk_finance_refunds_currency")
    op.execute("ALTER TABLE finance.refunds DROP COLUMN IF EXISTS currency_code")
