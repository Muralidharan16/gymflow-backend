"""PAY-3 durable ambiguity evidence and actor-identity fence.

Revision ID: zn07d8e9f0a48
Revises: zm07d8e9f0a47
Create Date: 2026-09-19

This repair is additive over the already-pushed PAY-3 migration. It does not
rewrite the predecessor revision. It preserves why a command entered UNKNOWN
after the command is later reconciled to a terminal result, and it prevents a
replay from silently changing the logical command's originating actor.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "zn07d8e9f0a48"
down_revision = "zm07d8e9f0a47"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_FINANCE_RUNTIME = "finance_runtime"
_TABLE = "finance.monetary_commands"
_RESERVE = "app_secure.reserve_finance_monetary_command(text,text,text,text,uuid,text,text)"
_MARK_UNKNOWN = "app_secure.mark_finance_monetary_command_unknown(uuid,text)"


def _require_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(sa.text(
        """
        SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
               rolreplication,rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname=:role
        """
    ), {"role": role}).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-3 repair missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-3 repair role login posture drift: {role}")
    for key in ("rolsuper","rolinherit","rolcreatedb","rolcreaterole","rolreplication","rolbypassrls"):
        if bool(row[key]):
            raise RuntimeError(f"PAY-3 repair reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _FINANCE_RUNTIME)
    identity = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-3 repair migration requires migration_owner")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
    ), {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER}).scalar_one():
        raise RuntimeError("PAY-3 repair requires migration_owner SET edge to app_security_owner")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"
    ), {"relation": _TABLE}).scalar_one():
        raise RuntimeError("PAY-3 repair requires predecessor monetary command table")


def _reserve_sql(*, actor_fence: bool) -> str:
    actor_clause = """
                       OR v_row.actor_type IS DISTINCT FROM p_actor_type
                       OR v_row.actor_ref_sha256::text IS DISTINCT FROM p_actor_ref_sha256""" if actor_fence else ""
    return rf"""
    CREATE OR REPLACE FUNCTION app_secure.reserve_finance_monetary_command(
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
        IF p_scope IS NULL OR p_scope !~ '^[a-z][a-z0-9_.]{{2,119}}$'
           OR p_idempotency_key IS NULL
           OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{{0,199}}$'
           OR p_request_hash_sha256 IS NULL
           OR p_request_hash_sha256 !~ '^[0-9a-f]{{64}}$'
           OR p_business_reference IS NULL
           OR p_business_reference !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{{0,199}}$'
           OR p_correlation_id IS NULL
           OR p_actor_type IS NULL
           OR p_actor_type !~ '^[a-z][a-z0-9_]{{0,39}}$'
           OR p_actor_ref_sha256 IS NULL
           OR p_actor_ref_sha256 !~ '^[0-9a-f]{{64}}$' THEN
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
               OR v_row.business_reference IS DISTINCT FROM p_business_reference{actor_clause} THEN
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


def _mark_unknown_sql(*, durable: bool) -> str:
    if durable:
        replay = """
        IF v_row.status='unknown' THEN
            IF v_row.ambiguity_code IS DISTINCT FROM p_error_code THEN
                RAISE EXCEPTION 'PAY-3 monetary unknown evidence conflict'
                    USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT v_row.id,v_row.status::text,
                v_row.ambiguity_code::text,true;
            RETURN;
        END IF;"""
        update = """
        UPDATE finance.monetary_commands c
        SET status='unknown',ambiguity_code=p_error_code,
            unknown_at=pg_catalog.clock_timestamp(),
            error_code=NULL,updated_at=pg_catalog.clock_timestamp()
        WHERE c.id=v_row.id
        RETURNING * INTO v_row;
        RETURN QUERY SELECT v_row.id,v_row.status::text,
            v_row.ambiguity_code::text,false;"""
    else:
        replay = """
        IF v_row.status='unknown' THEN
            RETURN QUERY SELECT v_row.id,v_row.status::text,
                p_error_code::text,true;
            RETURN;
        END IF;"""
        update = """
        UPDATE finance.monetary_commands c
        SET status='unknown',error_code=NULL,
            updated_at=pg_catalog.clock_timestamp()
        WHERE c.id=v_row.id
        RETURNING * INTO v_row;
        RETURN QUERY SELECT v_row.id,v_row.status::text,p_error_code::text,false;"""
    return rf"""
    CREATE OR REPLACE FUNCTION app_secure.mark_finance_monetary_command_unknown(
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
           OR p_error_code !~ '^[a-z][a-z0-9_]{{0,63}}$'
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
        {replay}
        IF v_row.status<>'processing' THEN
            RAISE EXCEPTION 'PAY-3 terminal monetary command cannot become unknown'
                USING ERRCODE='23514';
        END IF;
        {update}
    END
    $function$
    """


def _assert_function_posture(bind, signature: str) -> None:
    schema, rest = signature.split('.',1)
    name, args = rest.split('(',1)
    args = args.rstrip(')').replace(' ','')
    row = bind.execute(sa.text(
        """
        SELECT p.oid,r.rolname AS owner,p.prosecdef,p.proconfig
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
        WHERE n.nspname=:schema AND p.proname=:name
          AND replace(pg_catalog.oidvectortypes(p.proargtypes),' ','')=:args
        """
    ), {"schema":schema,"name":name,"args":args}).mappings().one_or_none()
    if row is None or row["owner"] != _SECURITY_OWNER or not row["prosecdef"]:
        raise RuntimeError(f"PAY-3 repair function posture drift: {signature}")
    cfg=set(row["proconfig"] or ())
    if "row_security=on" not in cfg or not any(v.startswith("search_path=") for v in cfg):
        raise RuntimeError(f"PAY-3 repair function config drift: {signature}")
    public_exec = bind.execute(sa.text(
        """
        SELECT EXISTS(
          SELECT 1 FROM pg_catalog.aclexplode(
            COALESCE(
              (SELECT proacl FROM pg_catalog.pg_proc WHERE oid=:oid),
              pg_catalog.acldefault('f',(SELECT proowner FROM pg_catalog.pg_proc WHERE oid=:oid))
            )
          ) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'
        )
        """
    ), {"oid":row["oid"]}).scalar_one()
    if public_exec:
        raise RuntimeError(f"PAY-3 repair PUBLIC EXECUTE leaked: {signature}")


def upgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    columns={r[0] for r in bind.execute(sa.text(
        "SELECT attname FROM pg_catalog.pg_attribute "
        "WHERE attrelid='finance.monetary_commands'::regclass AND attnum>0 AND NOT attisdropped"
    )).all()}
    if "ambiguity_code" in columns or "unknown_at" in columns:
        raise RuntimeError("PAY-3 repair ambiguity columns already exist")

    # The pushed predecessor did not persist why a command became UNKNOWN.
    # Never invent that evidence during migration.  A populated predecessor with
    # UNKNOWN rows requires an explicit evidence-recovery decision instead.
    if bind.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM finance.monetary_commands "
        "WHERE status='unknown' LIMIT 1)"
    )).scalar_one():
        raise RuntimeError(
            "PAY-3 repair cannot infer ambiguity evidence for pre-existing unknown command"
        )

    op.execute("ALTER TABLE finance.monetary_commands ADD COLUMN ambiguity_code VARCHAR(64) NULL")
    op.execute("ALTER TABLE finance.monetary_commands ADD COLUMN unknown_at TIMESTAMPTZ NULL")
    op.execute("""
        ALTER TABLE finance.monetary_commands
        ADD CONSTRAINT chk_finance_monetary_commands_ambiguity_code
        CHECK (
          ambiguity_code IS NULL OR (
            ambiguity_code ~ '^[a-z][a-z0-9_]{0,63}$'
            AND ambiguity_code !~ '(secret|token|bearer)'
          )
        )
    """)
    op.execute("""
        ALTER TABLE finance.monetary_commands
        ADD CONSTRAINT chk_finance_monetary_commands_ambiguity_pair
        CHECK ((ambiguity_code IS NULL) = (unknown_at IS NULL))
    """)
    op.execute("""
        ALTER TABLE finance.monetary_commands
        ADD CONSTRAINT chk_finance_monetary_commands_unknown_evidence
        CHECK (status <> 'unknown' OR (ambiguity_code IS NOT NULL AND unknown_at IS NOT NULL))
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(_reserve_sql(actor_fence=True))
        op.execute(_mark_unknown_sql(durable=True))
        op.execute(f"REVOKE ALL ON FUNCTION {_RESERVE} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {_MARK_UNKNOWN} FROM PUBLIC")
    finally:
        op.execute("RESET ROLE")

    _assert_function_posture(bind,_RESERVE)
    _assert_function_posture(bind,_MARK_UNKNOWN)

    for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER"):
        if bind.execute(sa.text(
            "SELECT pg_catalog.has_table_privilege(:role,:relation,:privilege)"
        ), {"role":_FINANCE_RUNTIME,"relation":_TABLE,"privilege":privilege}).scalar_one():
            raise RuntimeError(f"PAY-3 repair finance_runtime direct table privilege leaked: {privilege}")


def downgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        has_ambiguity=bind.execute(sa.text(
            "SELECT EXISTS(SELECT 1 FROM finance.monetary_commands "
            "WHERE ambiguity_code IS NOT NULL OR unknown_at IS NOT NULL LIMIT 1)"
        )).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if has_ambiguity:
        raise RuntimeError(
            "PAY-3 repair downgrade blocked: durable ambiguity evidence exists"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(_reserve_sql(actor_fence=False))
        op.execute(_mark_unknown_sql(durable=False))
        op.execute(f"REVOKE ALL ON FUNCTION {_RESERVE} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {_MARK_UNKNOWN} FROM PUBLIC")
    finally:
        op.execute("RESET ROLE")

    op.execute("ALTER TABLE finance.monetary_commands DROP CONSTRAINT chk_finance_monetary_commands_unknown_evidence")
    op.execute("ALTER TABLE finance.monetary_commands DROP CONSTRAINT chk_finance_monetary_commands_ambiguity_pair")
    op.execute("ALTER TABLE finance.monetary_commands DROP CONSTRAINT chk_finance_monetary_commands_ambiguity_code")
    op.execute("ALTER TABLE finance.monetary_commands DROP COLUMN unknown_at")
    op.execute("ALTER TABLE finance.monetary_commands DROP COLUMN ambiguity_code")
