"""PAY-3 durable monetary-command idempotency protocol.

Revision ID: zm07d8e9f0a47
Revises: zl07d8e9f0a46
Create Date: 2026-09-19

Creates persistent command evidence only. No live provider call, payment capture,
refund execution, subscription activation or existing Finance row rewrite is
introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zm07d8e9f0a47"
down_revision = "zl07d8e9f0a46"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_FINANCE_RUNTIME = "finance_runtime"
_TABLE = "finance.monetary_commands"
_POLICY = "pay3_monetary_commands_security_owner_all"
_FUNCTIONS = (
    "app_secure.reserve_finance_monetary_command(text,text,text,text,uuid,text,text)",
    "app_secure.complete_finance_monetary_command(uuid,text)",
    "app_secure.mark_finance_monetary_command_unknown(uuid,text)",
    "app_secure.fail_finance_monetary_command(uuid,text)",
)


def _require_reduced_role(bind, role_name: str, *, login: bool = False) -> None:
    row = bind.execute(sa.text(
        """
        SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
               rolreplication,rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname=:role
        """
    ), {"role": role_name}).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-3 missing externally managed role: {role_name}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-3 role login posture drift: {role_name}")
    for key in ("rolsuper","rolinherit","rolcreatedb","rolcreaterole","rolreplication","rolbypassrls"):
        if bool(row[key]):
            raise RuntimeError(f"PAY-3 reduced-role drift: {role_name}.{key}")


def _function_row(bind, signature: str):
    schema_name, remainder = signature.split(".", 1)
    function_name, args = remainder.split("(", 1)
    normalized_args = args.rstrip(")").replace(" ", "")
    return bind.execute(sa.text(
        """
        SELECT p.oid,r.rolname AS owner,p.prosecdef,p.proconfig
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
        WHERE n.nspname=:schema_name
          AND p.proname=:function_name
          AND replace(pg_catalog.oidvectortypes(p.proargtypes),' ','')=:args
        """
    ), {
        "schema_name": schema_name,
        "function_name": function_name,
        "args": normalized_args,
    }).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    _require_reduced_role(bind, _FINANCE_RUNTIME)
    identity = bind.execute(sa.text(
        "SELECT session_user::text,current_user::text"
    )).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-3 migration requires migration_owner")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
    ), {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER}).scalar_one():
        raise RuntimeError("PAY-3 requires migration_owner SET edge to app_security_owner")
    if bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"
    ), {"relation": _TABLE}).scalar_one():
        raise RuntimeError("PAY-3 monetary command table already exists")
    for signature in _FUNCTIONS:
        if _function_row(bind, signature) is not None:
            raise RuntimeError(f"PAY-3 function already exists: {signature}")
    if bind.execute(sa.text(
        "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
        "OR pg_catalog.pg_has_role(:member,:target,'SET')"
    ), {"member": _FINANCE_RUNTIME, "target": _SECURITY_OWNER}).scalar_one():
        raise RuntimeError("PAY-3 finance_runtime can reach app_security_owner")
    if bind.execute(sa.text(
        "SELECT pg_catalog.has_schema_privilege(:role,'finance','USAGE') "
        "OR pg_catalog.has_schema_privilege(:role,'finance','CREATE')"
    ), {"role": _FINANCE_RUNTIME}).scalar_one():
        raise RuntimeError("PAY-3 refuses pre-existing finance_runtime Finance schema authority")


def _install_schema() -> None:
    op.execute(
        """
        CREATE TABLE finance.monetary_commands (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            scope VARCHAR(120) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            request_hash_sha256 CHAR(64) NOT NULL,
            business_reference VARCHAR(200) NOT NULL,
            correlation_id UUID NOT NULL,
            actor_type VARCHAR(40) NOT NULL,
            actor_ref_sha256 CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            response_ref VARCHAR(200) NULL,
            error_code VARCHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NULL,
            CONSTRAINT uq_finance_monetary_commands_scope_key
                UNIQUE (organization_id, scope, idempotency_key),
            CONSTRAINT chk_finance_monetary_commands_scope
                CHECK (scope ~ '^[a-z][a-z0-9_.]{2,119}$'),
            CONSTRAINT chk_finance_monetary_commands_key
                CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'),
            CONSTRAINT chk_finance_monetary_commands_request_hash
                CHECK (request_hash_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_finance_monetary_commands_business_ref
                CHECK (business_reference ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'),
            CONSTRAINT chk_finance_monetary_commands_actor_type
                CHECK (actor_type ~ '^[a-z][a-z0-9_]{0,39}$'),
            CONSTRAINT chk_finance_monetary_commands_actor_hash
                CHECK (actor_ref_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_finance_monetary_commands_status
                CHECK (status IN ('processing','unknown','succeeded','failed_deterministic')),
            CONSTRAINT chk_finance_monetary_commands_error_code
                CHECK (
                    error_code IS NULL OR (
                        error_code ~ '^[a-z][a-z0-9_]{0,63}$'
                        AND error_code !~ '(secret|token|bearer)'
                    )
                ),
            CONSTRAINT chk_finance_monetary_commands_response_ref
                CHECK (
                    response_ref IS NULL
                    OR response_ref ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'
                ),
            CONSTRAINT chk_finance_monetary_commands_success_response
                CHECK ((status='succeeded') = (response_ref IS NOT NULL)),
            CONSTRAINT chk_finance_monetary_commands_failure_error
                CHECK ((status='failed_deterministic') = (error_code IS NOT NULL)),
            CONSTRAINT chk_finance_monetary_commands_terminal_completed
                CHECK (
                    (status IN ('succeeded','failed_deterministic'))
                    = (completed_at IS NOT NULL)
                )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_finance_monetary_commands_status_created "
        "ON finance.monetary_commands(status,created_at)"
    )
    op.execute("ALTER TABLE finance.monetary_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.monetary_commands FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.monetary_commands FROM PUBLIC")
    op.execute(
        "GRANT SELECT,INSERT,UPDATE ON TABLE finance.monetary_commands "
        "TO app_security_owner"
    )
    op.execute(
        """
        CREATE POLICY pay3_monetary_commands_security_owner_all
        ON finance.monetary_commands
        FOR ALL TO app_security_owner
        USING (true)
        WITH CHECK (true)
        """
    )


def _install_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.reserve_finance_monetary_command(
                p_scope text,
                p_idempotency_key text,
                p_request_hash_sha256 text,
                p_business_reference text,
                p_correlation_id uuid,
                p_actor_type text,
                p_actor_ref_sha256 text
            )
            RETURNS TABLE(
                command_id uuid,
                organization_id uuid,
                scope text,
                idempotency_key text,
                request_hash_sha256 text,
                business_reference text,
                correlation_id uuid,
                actor_type text,
                actor_ref_sha256 text,
                status text,
                response_ref text,
                error_code text,
                created_at timestamptz,
                updated_at timestamptz,
                completed_at timestamptz,
                inserted boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_row finance.monetary_commands%ROWTYPE;
                v_count integer := 0;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'finance_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-3 monetary command requires finance_runtime'
                        USING ERRCODE='42501';
                END IF;
                BEGIN
                    v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'PAY-3 monetary command requires valid tenant context'
                        USING ERRCODE='22023';
                END;
                IF v_org IS NULL THEN
                    RAISE EXCEPTION 'PAY-3 monetary command requires tenant context'
                        USING ERRCODE='22023';
                END IF;
                IF p_scope IS NULL OR p_scope !~ '^[a-z][a-z0-9_.]{2,119}$'
                   OR p_idempotency_key IS NULL
                   OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'
                   OR p_request_hash_sha256 IS NULL
                   OR p_request_hash_sha256 !~ '^[0-9a-f]{64}$'
                   OR p_business_reference IS NULL
                   OR p_business_reference !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'
                   OR p_correlation_id IS NULL
                   OR p_actor_type IS NULL
                   OR p_actor_type !~ '^[a-z][a-z0-9_]{0,39}$'
                   OR p_actor_ref_sha256 IS NULL
                   OR p_actor_ref_sha256 !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'PAY-3 monetary command identity invalid'
                        USING ERRCODE='22023';
                END IF;

                INSERT INTO finance.monetary_commands(
                    organization_id,scope,idempotency_key,request_hash_sha256,
                    business_reference,correlation_id,actor_type,actor_ref_sha256
                )
                VALUES (
                    v_org,p_scope,p_idempotency_key,p_request_hash_sha256::char(64),
                    p_business_reference,p_correlation_id,p_actor_type,
                    p_actor_ref_sha256::char(64)
                )
                ON CONFLICT ON CONSTRAINT uq_finance_monetary_commands_scope_key
                DO NOTHING
                RETURNING * INTO v_row;
                GET DIAGNOSTICS v_count = ROW_COUNT;

                IF v_count = 0 THEN
                    SELECT * INTO v_row
                    FROM finance.monetary_commands c
                    WHERE c.organization_id=v_org
                      AND c.scope=p_scope
                      AND c.idempotency_key=p_idempotency_key
                    FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'PAY-3 monetary command reservation disappeared'
                            USING ERRCODE='40001';
                    END IF;
                    IF v_row.request_hash_sha256::text IS DISTINCT FROM p_request_hash_sha256
                       OR v_row.business_reference IS DISTINCT FROM p_business_reference THEN
                        RAISE EXCEPTION 'PAY-3 monetary command request conflict'
                            USING ERRCODE='23505';
                    END IF;
                END IF;

                RETURN QUERY SELECT
                    v_row.id,v_row.organization_id,v_row.scope::text,
                    v_row.idempotency_key::text,v_row.request_hash_sha256::text,
                    v_row.business_reference::text,v_row.correlation_id,
                    v_row.actor_type::text,v_row.actor_ref_sha256::text,
                    v_row.status::text,v_row.response_ref::text,v_row.error_code::text,
                    v_row.created_at,v_row.updated_at,v_row.completed_at,
                    (v_count=1),(v_count=0);
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.complete_finance_monetary_command(
                p_command_id uuid,
                p_response_ref text
            )
            RETURNS TABLE(command_id uuid,status text,response_ref text,replayed boolean)
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_row finance.monetary_commands%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'finance_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-3 monetary completion requires finance_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_command_id IS NULL
                   OR p_response_ref IS NULL
                   OR p_response_ref !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$' THEN
                    RAISE EXCEPTION 'PAY-3 monetary completion invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT * INTO v_row
                FROM finance.monetary_commands c
                WHERE c.id=p_command_id AND c.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-3 monetary command unavailable'
                        USING ERRCODE='42501';
                END IF;
                IF v_row.status='succeeded' THEN
                    IF v_row.response_ref IS DISTINCT FROM p_response_ref THEN
                        RAISE EXCEPTION 'PAY-3 monetary terminal result conflict'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT v_row.id,v_row.status::text,
                        v_row.response_ref::text,true;
                    RETURN;
                END IF;
                IF v_row.status='failed_deterministic' THEN
                    RAISE EXCEPTION 'PAY-3 deterministic failure is terminal'
                        USING ERRCODE='23514';
                END IF;
                UPDATE finance.monetary_commands c
                SET status='succeeded',response_ref=p_response_ref,error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp(),
                    completed_at=pg_catalog.clock_timestamp()
                WHERE c.id=v_row.id
                RETURNING * INTO v_row;
                RETURN QUERY SELECT v_row.id,v_row.status::text,
                    v_row.response_ref::text,false;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.mark_finance_monetary_command_unknown(
                p_command_id uuid,
                p_error_code text
            )
            RETURNS TABLE(command_id uuid,status text,error_code text,replayed boolean)
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_row finance.monetary_commands%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'finance_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-3 monetary unknown requires finance_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_command_id IS NULL
                   OR p_error_code IS NULL
                   OR p_error_code !~ '^[a-z][a-z0-9_]{0,63}$'
                   OR p_error_code ~ '(secret|token|bearer)' THEN
                    RAISE EXCEPTION 'PAY-3 monetary unknown evidence invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT * INTO v_row
                FROM finance.monetary_commands c
                WHERE c.id=p_command_id AND c.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-3 monetary command unavailable'
                        USING ERRCODE='42501';
                END IF;
                IF v_row.status='unknown' THEN
                    RETURN QUERY SELECT v_row.id,v_row.status::text,
                        p_error_code::text,true;
                    RETURN;
                END IF;
                IF v_row.status<>'processing' THEN
                    RAISE EXCEPTION 'PAY-3 terminal monetary command cannot become unknown'
                        USING ERRCODE='23514';
                END IF;
                UPDATE finance.monetary_commands c
                SET status='unknown',error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.id=v_row.id
                RETURNING * INTO v_row;
                RETURN QUERY SELECT v_row.id,v_row.status::text,p_error_code::text,false;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.fail_finance_monetary_command(
                p_command_id uuid,
                p_error_code text
            )
            RETURNS TABLE(command_id uuid,status text,error_code text,replayed boolean)
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_row finance.monetary_commands%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'finance_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-3 monetary failure requires finance_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_command_id IS NULL
                   OR p_error_code IS NULL
                   OR p_error_code !~ '^[a-z][a-z0-9_]{0,63}$'
                   OR p_error_code ~ '(secret|token|bearer)' THEN
                    RAISE EXCEPTION 'PAY-3 monetary failure evidence invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT * INTO v_row
                FROM finance.monetary_commands c
                WHERE c.id=p_command_id AND c.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-3 monetary command unavailable'
                        USING ERRCODE='42501';
                END IF;
                IF v_row.status='failed_deterministic' THEN
                    IF v_row.error_code IS DISTINCT FROM p_error_code THEN
                        RAISE EXCEPTION 'PAY-3 deterministic failure conflict'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT v_row.id,v_row.status::text,
                        v_row.error_code::text,true;
                    RETURN;
                END IF;
                IF v_row.status='succeeded' THEN
                    RAISE EXCEPTION 'PAY-3 successful monetary command is terminal'
                        USING ERRCODE='23514';
                END IF;
                UPDATE finance.monetary_commands c
                SET status='failed_deterministic',response_ref=NULL,
                    error_code=p_error_code,
                    updated_at=pg_catalog.clock_timestamp(),
                    completed_at=pg_catalog.clock_timestamp()
                WHERE c.id=v_row.id
                RETURNING * INTO v_row;
                RETURN QUERY SELECT v_row.id,v_row.status::text,
                    v_row.error_code::text,false;
            END
            $function$
            """
        )
        for signature in _FUNCTIONS:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    finally:
        op.execute("RESET ROLE")


def _post_install(bind) -> None:
    enabled, forced = bind.execute(sa.text(
        "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
        "WHERE oid='finance.monetary_commands'::regclass"
    )).one()
    if not enabled or not forced:
        raise RuntimeError("PAY-3 monetary command table must retain ENABLE+FORCE RLS")

    for signature in _FUNCTIONS:
        row = _function_row(bind, signature)
        if row is None or row["owner"] != _SECURITY_OWNER or not row["prosecdef"]:
            raise RuntimeError(f"PAY-3 function owner/security drift: {signature}")
        config=set(row["proconfig"] or ())
        if "row_security=on" not in config or not any(v.startswith("search_path=") for v in config):
            raise RuntimeError(f"PAY-3 function configuration drift: {signature}")
        public_exec = bind.execute(sa.text(
            """
            SELECT EXISTS(
                SELECT 1 FROM pg_catalog.aclexplode(
                    COALESCE(
                        (SELECT proacl FROM pg_catalog.pg_proc WHERE oid=:oid),
                        pg_catalog.acldefault(
                            'f',(SELECT proowner FROM pg_catalog.pg_proc WHERE oid=:oid)
                        )
                    )
                ) acl
                WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
            )
            """
        ), {"oid": row["oid"]}).scalar_one()
        if public_exec:
            raise RuntimeError(f"PAY-3 PUBLIC EXECUTE leaked: {signature}")

    for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER"):
        if bind.execute(sa.text(
            "SELECT pg_catalog.has_table_privilege(:role,:relation,:privilege)"
        ), {
            "role": _FINANCE_RUNTIME,
            "relation": _TABLE,
            "privilege": privilege,
        }).scalar_one():
            raise RuntimeError(
                f"PAY-3 finance_runtime received direct monetary table {privilege}"
            )
    if bind.execute(sa.text(
        "SELECT pg_catalog.has_schema_privilege(:role,'finance','USAGE') "
        "OR pg_catalog.has_schema_privilege(:role,'finance','CREATE')"
    ), {"role": _FINANCE_RUNTIME}).scalar_one():
        raise RuntimeError("PAY-3 finance_runtime Finance schema authority leaked")


def upgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_predecessor(bind)
    _install_schema()
    _install_functions()
    _post_install(bind)


def downgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_reduced_role(bind,_MIGRATION_OWNER,login=True)
    identity=bind.execute(sa.text(
        "SELECT session_user::text,current_user::text"
    )).one()
    if tuple(identity)!=(_MIGRATION_OWNER,_MIGRATION_OWNER):
        raise RuntimeError("PAY-3 downgrade requires migration_owner")
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        has_evidence=bind.execute(sa.text(
            "SELECT EXISTS(SELECT 1 FROM finance.monetary_commands LIMIT 1)"
        )).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if has_evidence:
        raise RuntimeError(
            "PAY-3 downgrade blocked: durable monetary command evidence exists"
        )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in reversed(_FUNCTIONS):
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    finally:
        op.execute("RESET ROLE")
    op.execute(
        "DROP POLICY IF EXISTS pay3_monetary_commands_security_owner_all "
        "ON finance.monetary_commands"
    )
    op.execute("DROP TABLE finance.monetary_commands RESTRICT")
