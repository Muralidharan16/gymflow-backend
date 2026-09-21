"""PAY-16 security abuse hardening and tamper-evident Finance audit.

Revision ID: zz27d8e9f0a62
Revises: zz17d8e9f0a61
Create Date: 2026-09-21

Adds an organization-scoped append-only security audit chain for high-risk
Finance actions. Ordinary app_runtime receives no direct table DML; it may only
invoke one bounded SECURITY DEFINER append capability. Every row is chained to
the previous organization row under a transaction advisory lock.

Downgrade is allowed only while the PAY-16 audit table is empty. Once security
evidence exists, downgrade fails closed.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz27d8e9f0a62"
down_revision = "zz17d8e9f0a61"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_APP = "app_runtime"
_RECORD = (
    "app_secure.record_finance_security_audit("
    "text,text,uuid,text,text)"
)
_GUARD = "app_secure.pay16_reject_security_audit_mutation()"


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
        raise RuntimeError(f"PAY-16 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-16 role login posture drift: {role}")
    for key in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[key]):
            raise RuntimeError(f"PAY-16 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _APP)

    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-16 migration requires migration_owner")

    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
        {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "PAY-16 requires migration_owner SET edge to app_security_owner"
        )

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
            "OR pg_catalog.pg_has_role(:member,:target,'SET')"
        ),
        {"member": _APP, "target": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("PAY-16 app_runtime can reach app_security_owner")

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege("
            "'app_security_owner','finance','USAGE')"
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-16 predecessor missing app_security_owner Finance schema usage"
        )


def _install_table() -> None:
    op.execute(
        """
        CREATE TABLE finance.security_audit_events(
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            sequence_no BIGINT NOT NULL,
            actor_id UUID NOT NULL,
            actor_role VARCHAR(40) NOT NULL,
            event_type VARCHAR(120) NOT NULL,
            target_type VARCHAR(80) NOT NULL,
            target_id UUID NULL,
            reason_code VARCHAR(80) NULL,
            severity VARCHAR(16) NOT NULL,
            request_id VARCHAR(128) NOT NULL,
            previous_event_hash CHAR(64) NULL,
            event_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_pay16_security_audit_org_sequence
                UNIQUE(organization_id,sequence_no),
            CONSTRAINT chk_pay16_security_audit_sequence
                CHECK(sequence_no > 0),
            CONSTRAINT chk_pay16_security_audit_role
                CHECK(actor_role ~ '^[a-z][a-z0-9_]{0,39}$'),
            CONSTRAINT chk_pay16_security_audit_event
                CHECK(event_type ~ '^finance[.]security[.][a-z0-9_.-]{1,100}$'),
            CONSTRAINT chk_pay16_security_audit_target
                CHECK(target_type ~ '^[a-z][a-z0-9_.-]{0,79}$'),
            CONSTRAINT chk_pay16_security_audit_reason
                CHECK(
                    reason_code IS NULL
                    OR reason_code ~ '^[A-Z0-9][A-Z0-9_.:-]{2,79}$'
                ),
            CONSTRAINT chk_pay16_security_audit_severity
                CHECK(severity IN ('info','warning','critical')),
            CONSTRAINT chk_pay16_security_audit_request
                CHECK(char_length(request_id) BETWEEN 1 AND 128),
            CONSTRAINT chk_pay16_security_audit_prev_hash
                CHECK(
                    previous_event_hash IS NULL
                    OR previous_event_hash ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay16_security_audit_hash
                CHECK(event_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay16_security_audit_chain_shape
                CHECK(
                    (sequence_no=1 AND previous_event_hash IS NULL)
                    OR
                    (sequence_no>1 AND previous_event_hash IS NOT NULL)
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay16_security_audit_org_created
            ON finance.security_audit_events(
                organization_id,created_at DESC,sequence_no DESC
            )
        """
    )
    op.execute(
        "ALTER TABLE finance.security_audit_events ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE finance.security_audit_events FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        REVOKE ALL ON TABLE finance.security_audit_events FROM PUBLIC
        """
    )


def _install_owner_policies_and_functions(bind) -> None:
    # Table ACL and RLS policy DDL are owned by migration_owner. The security
    # owner receives only the exact table privileges its SECURITY DEFINER
    # append function needs.
    op.execute(
        """
        GRANT SELECT,INSERT
            ON TABLE finance.security_audit_events
            TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY pay16_security_audit_owner_select
            ON finance.security_audit_events
            FOR SELECT
            TO app_security_owner
            USING (
                organization_id =
                NULLIF(
                    pg_catalog.current_setting(
                        'app.current_org_id',true
                    ),
                    ''
                )::uuid
            )
        """
    )
    op.execute(
        """
        CREATE POLICY pay16_security_audit_owner_insert
            ON finance.security_audit_events
            FOR INSERT
            TO app_security_owner
            WITH CHECK (
                organization_id =
                NULLIF(
                    pg_catalog.current_setting(
                        'app.current_org_id',true
                    ),
                    ''
                )::uuid
            )
        """
    )

    had_create = bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege("
            "'app_security_owner','app_secure','CREATE')"
        )
    ).scalar_one()
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app_secure TO app_security_owner")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION
                app_secure.pay16_reject_security_audit_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            AS $function$
            BEGIN
                RAISE EXCEPTION
                    'PAY-16 Finance security audit is append-only'
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION
                app_secure.record_finance_security_audit(
                    p_event_type text,
                    p_target_type text,
                    p_target_id uuid,
                    p_reason_code text,
                    p_severity text
                )
            RETURNS uuid
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            AS $function$
            DECLARE
                v_org uuid;
                v_actor uuid;
                v_role text;
                v_request_id text;
                v_prev_hash text;
                v_sequence bigint;
                v_event_id uuid := pg_catalog.gen_random_uuid();
                v_event text;
                v_target text;
                v_reason text;
                v_severity text;
                v_payload jsonb;
                v_hash text;
            BEGIN
                BEGIN
                    v_org := NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',true
                        ),
                        ''
                    )::uuid;
                    v_actor := NULLIF(
                        pg_catalog.current_setting(
                            'app.current_user_id',true
                        ),
                        ''
                    )::uuid;
                EXCEPTION
                    WHEN invalid_text_representation THEN
                        RAISE EXCEPTION
                            'PAY-16 invalid Finance security identity context'
                            USING ERRCODE='42501';
                END;

                v_role := lower(
                    btrim(
                        COALESCE(
                            pg_catalog.current_setting(
                                'app.current_role',true
                            ),
                            ''
                        )
                    )
                );
                v_request_id := btrim(
                    COALESCE(
                        NULLIF(
                            pg_catalog.current_setting(
                                'app.request_id',true
                            ),
                            ''
                        ),
                        NULLIF(
                            pg_catalog.current_setting(
                                'application_name',true
                            ),
                            ''
                        ),
                        'unknown'
                    )
                );
                v_event := lower(btrim(COALESCE(p_event_type,'')));
                v_target := lower(btrim(COALESCE(p_target_type,'')));
                v_reason := upper(btrim(COALESCE(p_reason_code,'')));
                v_severity := lower(btrim(COALESCE(p_severity,'')));

                IF v_org IS NULL OR v_actor IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit requires tenant/actor context'
                        USING ERRCODE='42501';
                END IF;
                IF v_role NOT IN ('owner','admin') THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit actor role forbidden'
                        USING ERRCODE='42501';
                END IF;
                IF v_event !~ '^finance[.]security[.][a-z0-9_.-]{1,100}$' THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit event invalid'
                        USING ERRCODE='22023';
                END IF;
                IF v_target !~ '^[a-z][a-z0-9_.-]{0,79}$' THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit target invalid'
                        USING ERRCODE='22023';
                END IF;
                IF v_reason='' THEN
                    v_reason := NULL;
                ELSIF v_reason !~ '^[A-Z0-9][A-Z0-9_.:-]{2,79}$' THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit reason invalid'
                        USING ERRCODE='22023';
                END IF;
                IF v_severity NOT IN ('info','warning','critical') THEN
                    RAISE EXCEPTION
                        'PAY-16 Finance security audit severity invalid'
                        USING ERRCODE='22023';
                END IF;
                IF char_length(v_request_id)>128 THEN
                    v_request_id := left(v_request_id,128);
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(v_org::text,160016)
                );

                SELECT sequence_no,event_hash
                  INTO v_sequence,v_prev_hash
                  FROM finance.security_audit_events
                 WHERE organization_id=v_org
                 ORDER BY sequence_no DESC
                 LIMIT 1;

                v_sequence := COALESCE(v_sequence,0)+1;

                v_payload := pg_catalog.jsonb_build_object(
                    'organization_id',v_org::text,
                    'sequence_no',v_sequence,
                    'actor_id',v_actor::text,
                    'actor_role',v_role,
                    'event_type',v_event,
                    'target_type',v_target,
                    'target_id',
                        CASE
                            WHEN p_target_id IS NULL THEN NULL
                            ELSE p_target_id::text
                        END,
                    'reason_code',v_reason,
                    'severity',v_severity,
                    'request_id',v_request_id,
                    'previous_event_hash',v_prev_hash
                );
                v_hash := pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(v_payload::text,'utf8')
                    ),
                    'hex'
                );

                INSERT INTO finance.security_audit_events(
                    id,organization_id,sequence_no,
                    actor_id,actor_role,event_type,
                    target_type,target_id,reason_code,
                    severity,request_id,previous_event_hash,event_hash
                ) VALUES (
                    v_event_id,v_org,v_sequence,
                    v_actor,v_role,v_event,
                    v_target,p_target_id,v_reason,
                    v_severity,v_request_id,v_prev_hash,v_hash
                );

                RETURN v_event_id;
            END
            $function$
            """
        )
        for signature in (_GUARD, _RECORD):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_RECORD} TO app_runtime")

        # The table owner needs only a temporary schema-usage + execute
        # window to bind the immutable trigger. Both are revoked immediately
        # after trigger creation.
        op.execute("GRANT USAGE ON SCHEMA app_secure TO migration_owner")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_GUARD} TO migration_owner")
    finally:
        op.execute("RESET ROLE")

    if not had_create:
        op.execute("REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner")


def _install_immutable_trigger() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_pay16_security_audit_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE
        ON finance.security_audit_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION
            app_secure.pay16_reject_security_audit_mutation()
        """
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_GUARD} FROM migration_owner"
        )
        op.execute("REVOKE USAGE ON SCHEMA app_secure FROM migration_owner")
    finally:
        op.execute("RESET ROLE")


def _postflight(bind) -> None:
    for privilege in (
        "SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER"
    ):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_runtime','finance.security_audit_events',:privilege)"
            ),
            {"privilege": privilege},
        ).scalar_one():
            raise RuntimeError(
                "PAY-16 leaked direct app_runtime Finance security-audit "
                f"privilege: {privilege}"
            )

    schema_access = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                'app_runtime',
                namespace_data.oid,
                'USAGE'
            )
            FROM pg_catalog.pg_namespace AS namespace_data
            WHERE namespace_data.nspname = 'app_secure'
            """
        )
    ).scalar_one_or_none()
    if schema_access is not True:
        raise RuntimeError(
            "PAY-16 app_runtime missing app_secure schema usage"
        )

    function_access = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_function_privilege(
                'app_runtime',
                procedure_data.oid,
                'EXECUTE'
            )
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = procedure_data.pronamespace
            WHERE namespace_data.nspname = 'app_secure'
              AND procedure_data.proname =
                  'record_finance_security_audit'
              AND pg_catalog.oidvectortypes(
                      procedure_data.proargtypes
                  ) = 'text, text, uuid, text, text'
            """
        )
    ).scalar_one_or_none()
    if function_access is not True:
        raise RuntimeError(
            "PAY-16 app_runtime missing exact security-audit append capability"
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.to_regclass("
            "'finance.security_audit_events') IS NOT NULL"
        )
    ).scalar_one():
        raise RuntimeError("PAY-16 security audit table already exists")

    _install_table()
    _install_owner_policies_and_functions(bind)
    _install_immutable_trigger()
    _postflight(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    # Global downgrade guard: migration_owner owns the relation. Temporarily
    # lift FORCE RLS only for this owner inspection, then restore it before
    # making the downgrade decision.
    op.execute(
        "ALTER TABLE finance.security_audit_events "
        "NO FORCE ROW LEVEL SECURITY"
    )
    try:
        has_evidence = bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM finance.security_audit_events
                    LIMIT 1
                )
                """
            )
        ).scalar_one()
    finally:
        op.execute(
            "ALTER TABLE finance.security_audit_events "
            "FORCE ROW LEVEL SECURITY"
        )

    if has_evidence:
        raise RuntimeError(
            "PAY-16 downgrade blocked: Finance security audit evidence exists"
        )

    op.execute(
        "DROP TRIGGER trg_pay16_security_audit_immutable "
        "ON finance.security_audit_events"
    )
    op.execute(
        "DROP POLICY pay16_security_audit_owner_insert "
        "ON finance.security_audit_events"
    )
    op.execute(
        "DROP POLICY pay16_security_audit_owner_select "
        "ON finance.security_audit_events"
    )
    op.execute(
        "REVOKE SELECT,INSERT "
        "ON TABLE finance.security_audit_events "
        "FROM app_security_owner"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (_RECORD, _GUARD):
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    finally:
        op.execute("RESET ROLE")

    op.execute("DROP TABLE finance.security_audit_events")
