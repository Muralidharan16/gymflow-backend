"""PAY-24-C entitlement/subscription mutation authority.

Revision ID: zza7d8e9f0a70
Revises: zz97d8e9f0a69
Create Date: 2026-10-03

PAY-24-C keeps Finance facts separate from member entitlement effects.  Ordinary
workers may durably enqueue entitlement commands but may not mutate canonical
subscription state.  Only the externally managed entitlement_runtime may apply
commands through app_secure SECURITY DEFINER capabilities.

No provider I/O, live-money enablement, or Stage-1 activation is introduced.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zza7d8e9f0a70"
down_revision = "zz97d8e9f0a69"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_ENTITLEMENT_RUNTIME = "entitlement_runtime"
_COMMANDS = "public.member_entitlement_commands"
_FUNCTION_SNAPSHOT = "app_private.pay24c_predecessor_functions"
_ACL_SNAPSHOT = "app_private.pay24c_acl_snapshot"

_PAY4_APPLY = "app_secure.apply_member_subscription_finance_event(uuid,text)"
_PAY4_TERM_GUARD = "app_secure.pay4_guard_subscription_activation()"
_PAY4_V2_GUARD = "app_secure.pay4_guard_v2_activation()"
_PAY5_CONSUME = "app_secure.consume_member_subscription_finance_event(uuid,uuid,bigint)"

_RUNTIME_ROLES = (
    "app_runtime",
    "app_user",
    "auth_runtime",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
    "finance_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
    "finance_reconciliation_runtime",
    "finance_read_runtime",
    "finance_maintenance_runtime",
    "finance_config_runtime",
    _ENTITLEMENT_RUNTIME,
)

_REVOKE_SNAPSHOT = (
    ("app_user", "public.member_subscriptions", "INSERT"),
    ("app_user", "public.member_subscriptions", "UPDATE"),
    ("app_user", "public.member_subscriptions", "DELETE"),
    ("app_runtime", "public.member_subscriptions", "INSERT"),
    ("app_runtime", "public.member_subscriptions", "UPDATE"),
    ("app_runtime", "public.member_subscriptions", "DELETE"),
    ("app_user", "public.member_subscriptions_v2", "INSERT"),
    ("app_user", "public.member_subscriptions_v2", "UPDATE"),
    ("app_user", "public.member_subscriptions_v2", "DELETE"),
    ("app_user", "public.subscription_terms", "INSERT"),
    ("app_user", "public.subscription_terms", "UPDATE"),
    ("app_user", "public.subscription_terms", "DELETE"),
    ("app_user", "public.subscription_freezes", "INSERT"),
    ("app_user", "public.subscription_freezes", "UPDATE"),
    ("app_user", "public.subscription_freezes", "DELETE"),
    ("app_user", "public.subscription_events", "INSERT"),
    ("app_user", "public.subscription_events", "UPDATE"),
    ("app_user", "public.subscription_events", "DELETE"),
)


def _identity(bind) -> tuple[str, str]:
    row = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    return str(row[0]), str(row[1])


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
        raise RuntimeError(f"PAY24-C missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY24-C login posture drift: {role}")
    for field in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[field]):
            raise RuntimeError(f"PAY24-C reduced-role drift: {role}.{field}")


def _require_prerequisites(bind) -> None:
    if _identity(bind) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError(
            "PAY24-C migration requires session_user=current_user=migration_owner"
        )
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    for role in _RUNTIME_ROLES:
        _require_role(bind, role)

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                ":member,:target,'SET')"
            ),
            {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY24-C requires bounded migration_owner SET edge to app_security_owner"
        )

    for role in _RUNTIME_ROLES:
        if role == _ENTITLEMENT_RUNTIME:
            continue
        for semantic in ("MEMBER", "SET"):
            if bool(
                bind.execute(
                    sa.text(
                        "SELECT pg_catalog.pg_has_role("
                        ":role,:target,:semantic)"
                    ),
                    {
                        "role": role,
                        "target": _ENTITLEMENT_RUNTIME,
                        "semantic": semantic,
                    },
                ).scalar_one()
            ):
                raise RuntimeError(
                    "PAY24-C entitlement runtime isolation drift: "
                    f"{role} --{semantic}--> {_ENTITLEMENT_RUNTIME}"
                )

    for role in _RUNTIME_ROLES:
        if bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.pg_has_role("
                    ":role,:owner,'MEMBER') OR "
                    "pg_catalog.pg_has_role(:role,:owner,'SET')"
                ),
                {"role": role, "owner": _SECURITY_OWNER},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"PAY24-C runtime may reach security owner: {role}"
            )

    head = bind.execute(
        sa.text("SELECT version_num::text FROM alembic_version")
    ).scalar_one()
    if head != down_revision:
        raise RuntimeError(
            f"PAY24-C predecessor drift: expected {down_revision}, found {head}"
        )


def _create_snapshots(bind) -> None:
    for relation in (_FUNCTION_SNAPSHOT, _ACL_SNAPSHOT, _COMMANDS):
        if bool(
            bind.execute(
                sa.text("SELECT pg_catalog.to_regclass(:name) IS NOT NULL"),
                {"name": relation},
            ).scalar_one()
        ):
            raise RuntimeError(f"PAY24-C predecessor relation already exists: {relation}")

    op.execute(
        """
        CREATE TABLE app_private.pay24c_predecessor_functions(
            signature text PRIMARY KEY,
            definition text NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app_private.pay24c_acl_snapshot(
            grantee text NOT NULL,
            relation_name text NOT NULL,
            privilege_name text NOT NULL,
            was_present boolean NOT NULL,
            PRIMARY KEY(grantee,relation_name,privilege_name)
        )
        """
    )
    for grantee, relation, privilege in _REVOKE_SNAPSHOT:
        present = bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    ":grantee,:relation,:privilege)"
                ),
                {
                    "grantee": grantee,
                    "relation": relation,
                    "privilege": privilege,
                },
            ).scalar_one()
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.pay24c_acl_snapshot(
                    grantee,relation_name,privilege_name,was_present
                ) VALUES(:grantee,:relation,:privilege,:present)
                """
            ),
            {
                "grantee": grantee,
                "relation": relation,
                "privilege": privilege,
                "present": present,
            },
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _PAY4_APPLY,
            _PAY4_TERM_GUARD,
            _PAY4_V2_GUARD,
            _PAY5_CONSUME,
        ):
            definition = bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.pg_get_functiondef(
                        pg_catalog.to_regprocedure(:signature)
                    )
                    """
                ),
                {"signature": signature},
            ).scalar_one_or_none()
            if not definition:
                raise RuntimeError(
                    f"PAY24-C predecessor function unavailable: {signature}"
                )
            op.execute("RESET ROLE")
            bind.execute(
                sa.text(
                    """
                    INSERT INTO app_private.pay24c_predecessor_functions(
                        signature,definition
                    ) VALUES(:signature,:definition)
                    """
                ),
                {"signature": signature, "definition": str(definition)},
            )
            op.execute("SET LOCAL ROLE app_security_owner")
    finally:
        op.execute("RESET ROLE")


def _revoke_legacy_direct_writes() -> None:
    for role, relation, privilege in _REVOKE_SNAPSHOT:
        op.execute(
            f"REVOKE {privilege} ON TABLE {relation} FROM {role}"
        )


def _install_command_table() -> None:
    op.execute(
        """
        CREATE TABLE public.member_entitlement_commands(
            command_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id uuid NOT NULL,
            target_term_id uuid NOT NULL,
            command_type text NOT NULL,
            source_kind text NOT NULL,
            source_event_id uuid NULL,
            source_event_type text NULL,
            idempotency_key varchar(200) NOT NULL,
            requested_by uuid NULL,
            reason text NULL,
            requested_until date NULL,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 15,
            leased_by uuid NULL,
            leased_until timestamptz NULL,
            lease_fence bigint NOT NULL DEFAULT 0,
            result_status text NULL,
            result_event_id uuid NULL,
            last_error_code varchar(80) NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            started_at timestamptz NULL,
            completed_at timestamptz NULL,
            updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT fk_pay24c_command_term_org
                FOREIGN KEY(target_term_id,organization_id)
                REFERENCES public.subscription_terms(id,org_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay24c_command_idempotency
                UNIQUE(organization_id,idempotency_key),
            CONSTRAINT chk_pay24c_command_type
                CHECK(command_type IN (
                    'activate_paid','recompute_refund',
                    'cancel','freeze','resume','restore','extend'
                )),
            CONSTRAINT chk_pay24c_command_source
                CHECK(source_kind IN ('finance','refund','admin','reconciliation')),
            CONSTRAINT chk_pay24c_command_status
                CHECK(status IN (
                    'pending','processing','succeeded','failed','review_required'
                )),
            CONSTRAINT chk_pay24c_command_attempts
                CHECK(attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100),
            CONSTRAINT chk_pay24c_command_lease
                CHECK(
                    (status='processing' AND leased_by IS NOT NULL AND leased_until IS NOT NULL)
                    OR
                    (status<>'processing' AND leased_by IS NULL AND leased_until IS NULL)
                ),
            CONSTRAINT chk_pay24c_command_key
                CHECK(idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$')
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_pay24c_command_source
        ON public.member_entitlement_commands(
            source_event_id,target_term_id,command_type
        )
        WHERE source_event_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay24c_command_claim
        ON public.member_entitlement_commands(status,leased_until,created_at,command_id)
        WHERE status IN ('pending','processing')
        """
    )
    op.execute("ALTER TABLE public.member_entitlement_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.member_entitlement_commands FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.member_entitlement_commands FROM PUBLIC")
    op.execute(
        "GRANT SELECT,INSERT,UPDATE ON TABLE "
        "public.member_entitlement_commands TO app_security_owner"
    )
    op.execute(
        """
        CREATE POLICY pay24c_commands_security_owner
        ON public.member_entitlement_commands
        FOR ALL TO app_security_owner
        USING (
            organization_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
            OR pg_catalog.current_setting(
                'app.internal_maintenance',true
            )='pay24c_entitlement_claim'
        )
        WITH CHECK (
            organization_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
            OR pg_catalog.current_setting(
                'app.internal_maintenance',true
            )='pay24c_entitlement_claim'
        )
        """
    )

    # PAY-24-C needs cancellation/freeze/term-extension columns through the
    # security owner only. Runtime roles receive no direct DML.
    op.execute(
        """
        GRANT UPDATE (
            effective_ends_on,status,activated_at,expired_at,
            cancelled_at,cancelled_by,cancellation_reason,
            terminated_at,terminated_by,termination_reason,
            voided_at,voided_by,void_reason,updated_at,version
        ) ON TABLE public.subscription_terms TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT UPDATE (end_date,status,cancelled_at,updated_at,updated_by)
        ON TABLE public.member_subscriptions_v2 TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE
        ON TABLE public.subscription_freezes TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY pay24c_subscription_freezes_security_owner
        ON public.subscription_freezes
        FOR ALL TO app_security_owner
        USING (
            org_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
        )
        WITH CHECK (
            org_id = NULLIF(
                pg_catalog.current_setting('app.current_org_id',true),''
            )::uuid
        )
        """
    )


def _replace_predecessor_authority(bind) -> None:
    definitions = {
        str(row[0]): str(row[1])
        for row in bind.execute(
            sa.text(
                """
                SELECT signature,definition
                FROM app_private.pay24c_predecessor_functions
                """
            )
        ).all()
    }

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            _PAY4_APPLY,
            _PAY4_TERM_GUARD,
            _PAY4_V2_GUARD,
        ):
            definition = definitions[signature]
            if "worker_runtime" not in definition:
                raise RuntimeError(
                    f"PAY24-C predecessor authority token missing: {signature}"
                )
            successor = definition.replace(
                "worker_runtime", "entitlement_runtime"
            ).replace(
                "Finance worker authority", "PAY24-C entitlement authority"
            )
            op.execute(successor)
    finally:
        op.execute("RESET ROLE")


def _install_guards() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_guard_term_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN
                    RETURN NEW;
                END IF;
                IF NOT (
                    current_user='app_security_owner'
                    AND pg_catalog.pg_has_role(
                        session_user,'entitlement_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C protected term mutation requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_guard_v2_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN
                    RETURN NEW;
                END IF;
                IF NOT (
                    current_user='app_security_owner'
                    AND pg_catalog.pg_has_role(
                        session_user,'entitlement_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C protected V2 mutation requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_guard_freeze_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN
                    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
                END IF;
                IF NOT (
                    current_user='app_security_owner'
                    AND pg_catalog.pg_has_role(
                        session_user,'entitlement_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C freeze mutation requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
            END
            $function$
            """
        )
        for signature in (
            "app_secure.pay24c_guard_term_mutation()",
            "app_secure.pay24c_guard_v2_mutation()",
            "app_secure.pay24c_guard_freeze_mutation()",
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    finally:
        op.execute("RESET ROLE")

    # migration_owner deliberately has no app_secure USAGE.  Create the
    # trigger bindings through the reduced function owner instead of widening
    # migration_owner's schema reachability.  The table-owner grants are
    # transaction-local in effect because this revision is atomic and are
    # explicitly revoked before the migration can succeed.
    for relation in (
        "public.subscription_terms",
        "public.member_subscriptions_v2",
        "public.subscription_freezes",
    ):
        op.execute(
            f"GRANT TRIGGER ON TABLE {relation} TO app_security_owner"
        )

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            """
            CREATE TRIGGER trg_pay24c_term_mutation_guard
            BEFORE UPDATE OF
                effective_ends_on,status,activated_at,expired_at,
                cancelled_at,cancelled_by,cancellation_reason,
                terminated_at,terminated_by,termination_reason,
                voided_at,voided_by,void_reason
            ON public.subscription_terms
            FOR EACH ROW EXECUTE FUNCTION app_secure.pay24c_guard_term_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_pay24c_v2_mutation_guard
            BEFORE UPDATE OF end_date,status,cancelled_at
            ON public.member_subscriptions_v2
            FOR EACH ROW EXECUTE FUNCTION app_secure.pay24c_guard_v2_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_pay24c_freeze_mutation_guard
            BEFORE INSERT OR UPDATE OR DELETE
            ON public.subscription_freezes
            FOR EACH ROW EXECUTE FUNCTION app_secure.pay24c_guard_freeze_mutation()
            """
        )
    finally:
        op.execute("RESET ROLE")

    for relation in (
        "public.subscription_terms",
        "public.member_subscriptions_v2",
        "public.subscription_freezes",
    ):
        op.execute(
            f"REVOKE TRIGGER ON TABLE {relation} FROM app_security_owner"
        )


def _install_enqueue_and_pay5_successor() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_enqueue_paid_activation(
                p_finance_event_id uuid,
                p_idempotency_key text
            )
            RETURNS TABLE(
                command_id uuid,
                subscription_term_id uuid,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_invoice uuid;
                v_binding finance.member_subscription_finance_bindings%ROWTYPE;
                v_term public.subscription_terms%ROWTYPE;
                v_command public.member_entitlement_commands%ROWTYPE;
                v_inserted boolean:=false;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C paid activation enqueue requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL
                   OR p_finance_event_id IS NULL
                   OR p_idempotency_key IS NULL
                   OR p_idempotency_key !~
                      '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C paid activation enqueue identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.aggregate_id INTO v_invoice
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='invoice'
                  AND e.event_type='finance.invoice.paid';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C activation requires authoritative invoice-paid event'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_binding
                FROM finance.member_subscription_finance_bindings b
                WHERE b.organization_id=v_org
                  AND b.finance_invoice_id=v_invoice;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C activation binding unavailable'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_term
                FROM public.subscription_terms t
                WHERE t.id=v_binding.subscription_term_id
                  AND t.org_id=v_org;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C activation term unavailable'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO public.member_entitlement_commands(
                    organization_id,target_term_id,command_type,
                    source_kind,source_event_id,source_event_type,
                    idempotency_key,payload_json
                ) VALUES (
                    v_org,v_term.id,'activate_paid',
                    'finance',p_finance_event_id,'finance.invoice.paid',
                    p_idempotency_key,
                    pg_catalog.jsonb_build_object(
                        'finance_invoice_id',v_invoice::text,
                        'finance_binding_id',v_binding.id::text
                    )
                )
                ON CONFLICT (organization_id,idempotency_key) DO NOTHING
                RETURNING * INTO v_command;

                IF FOUND THEN
                    v_inserted:=true;
                ELSE
                    SELECT * INTO STRICT v_command
                    FROM public.member_entitlement_commands c
                    WHERE c.organization_id=v_org
                      AND c.idempotency_key=p_idempotency_key;
                    IF v_command.target_term_id<>v_term.id
                       OR v_command.command_type<>'activate_paid'
                       OR v_command.source_event_id IS DISTINCT FROM p_finance_event_id
                       OR v_command.source_event_type IS DISTINCT FROM 'finance.invoice.paid'
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C activation idempotency identity conflict'
                            USING ERRCODE='23505';
                    END IF;
                END IF;

                RETURN QUERY SELECT
                    v_command.command_id,
                    v_command.target_term_id,
                    v_command.status,
                    NOT v_inserted;
            END
            $function$
            """
        )

        # PAY-5 successor: worker commits only the durable entitlement command
        # and consumption evidence. It never calls the mutation authority.
        op.execute(
            r"""
            CREATE OR REPLACE FUNCTION app_secure.consume_member_subscription_finance_event(
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
                v_enqueued record;
                v_consume_key text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION
                        'PAY-5 Finance-event consume requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL
                   OR p_worker_id IS NULL OR p_lease_fence IS NULL
                   OR p_lease_fence < 1
                THEN
                    RAISE EXCEPTION
                        'PAY-5 Finance-event consume identity invalid'
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
                    RAISE EXCEPTION
                        'PAY-5 Finance-event lease is not owned by this worker/fence'
                        USING ERRCODE='40001';
                END IF;

                v_consume_key:='pay5:member-subscription:'||
                    p_finance_event_id::text;

                SELECT * INTO v_consumption
                FROM public.member_subscription_finance_event_consumptions c
                WHERE c.finance_event_id=p_finance_event_id
                   OR c.idempotency_key=v_consume_key;
                IF FOUND THEN
                    IF v_consumption.finance_event_id
                           IS DISTINCT FROM p_finance_event_id
                       OR v_consumption.org_id IS DISTINCT FROM v_org
                       OR v_consumption.idempotency_key
                           IS DISTINCT FROM v_consume_key
                       OR v_consumption.finance_payload_sha256
                           IS DISTINCT FROM v_event.payload_sha256
                    THEN
                        RAISE EXCEPTION
                            'PAY-5 consumed Finance-event identity conflict'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_consumption.subscription_term_id,
                        v_consumption.business_result_status,
                        false,true,v_consumption.id;
                    RETURN;
                END IF;

                SELECT * INTO v_enqueued
                FROM app_secure.pay24c_enqueue_paid_activation(
                    p_finance_event_id,v_consume_key
                );

                INSERT INTO public.member_subscription_finance_event_consumptions(
                    org_id,finance_event_id,idempotency_key,
                    finance_payload_sha256,subscription_term_id,
                    business_result_status,effect_applied
                ) VALUES (
                    v_org,p_finance_event_id,v_consume_key,
                    v_event.payload_sha256,v_enqueued.subscription_term_id,
                    'entitlement_pending',false
                )
                RETURNING * INTO v_consumption;

                RETURN QUERY SELECT
                    v_consumption.subscription_term_id,
                    v_consumption.business_result_status,
                    false,false,v_consumption.id;
            END
            $function$
            """
        )

        for signature, role in (
            (
                "app_secure.pay24c_enqueue_paid_activation(uuid,text)",
                "worker_runtime",
            ),
            (_PAY5_CONSUME, "worker_runtime"),
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {_ENTITLEMENT_RUNTIME}")
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")
    finally:
        op.execute("RESET ROLE")


def _install_refund_enqueue() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_claim_refund_events(
                p_worker_id uuid,
                p_batch_size integer,
                p_lease_seconds integer
            )
            RETURNS TABLE(
                finance_event_id uuid,
                organization_id uuid,
                refund_id uuid,
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
                        'PAY-24-C refund-event claim requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL
                   OR p_batch_size IS NULL OR p_batch_size<1 OR p_batch_size>100
                   OR p_lease_seconds IS NULL OR p_lease_seconds<30
                   OR p_lease_seconds>3600
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event claim arguments invalid'
                        USING ERRCODE='22023';
                END IF;

                RETURN QUERY
                WITH candidates AS (
                    SELECT e.id,(e.status='processing') AS reclaiming
                    FROM finance.outbox_events e
                    WHERE e.organization_id IS NOT NULL
                      AND e.aggregate_type='refund'
                      AND e.event_type='finance.refund.completed'
                      AND (
                        (e.status='pending' AND e.attempt_count<e.max_attempts)
                        OR
                        (e.status='processing'
                         AND e.leased_until<=pg_catalog.clock_timestamp())
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
                RETURNING e.id,e.organization_id,e.aggregate_id,
                          e.attempt_count,e.max_attempts,e.lease_fence;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_consume_refund_event(
                p_finance_event_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint
            )
            RETURNS integer
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_event record;
                v_refund finance.refunds%ROWTYPE;
                v_count integer:=0;
                v_row record;
                v_key text;
                v_command public.member_entitlement_commands%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event consume requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL
                   OR p_worker_id IS NULL OR p_lease_fence IS NULL
                   OR p_lease_fence<1
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event consume identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.id,e.aggregate_id,e.status,e.leased_by,
                       e.leased_until,e.lease_fence
                INTO v_event
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='refund'
                  AND e.event_type='finance.refund.completed'
                FOR UPDATE;
                IF NOT FOUND
                   OR v_event.status<>'processing'
                   OR v_event.leased_by IS DISTINCT FROM p_worker_id
                   OR v_event.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_event.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event lease/fence lost'
                        USING ERRCODE='40001';
                END IF;

                SELECT * INTO v_refund
                FROM finance.refunds r
                WHERE r.id=v_event.aggregate_id
                  AND r.organization_id=v_org
                  AND r.status='succeeded';
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund event lacks succeeded refund truth'
                        USING ERRCODE='23514';
                END IF;

                FOR v_row IN
                    SELECT DISTINCT b.subscription_term_id
                    FROM finance.payment_allocations a
                    JOIN finance.member_subscription_finance_bindings b
                      ON b.finance_invoice_id=a.invoice_id
                     AND b.organization_id=v_org
                    WHERE a.payment_id=v_refund.payment_id
                LOOP
                    v_key:='pay24c:refund:'||p_finance_event_id::text||
                        ':'||v_row.subscription_term_id::text;
                    INSERT INTO public.member_entitlement_commands(
                        organization_id,target_term_id,command_type,
                        source_kind,source_event_id,source_event_type,
                        idempotency_key,payload_json
                    ) VALUES (
                        v_org,v_row.subscription_term_id,'recompute_refund',
                        'refund',p_finance_event_id,'finance.refund.completed',
                        v_key,
                        pg_catalog.jsonb_build_object(
                            'refund_id',v_refund.id::text,
                            'payment_id',v_refund.payment_id::text
                        )
                    )
                    ON CONFLICT (organization_id,idempotency_key) DO NOTHING
                    RETURNING * INTO v_command;
                    v_count:=v_count+1;
                END LOOP;

                RETURN v_count;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_ack_refund_event(
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
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event ack requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event ack tenant missing'
                        USING ERRCODE='22023';
                END IF;

                UPDATE finance.outbox_events e
                SET status='published',
                    published_at=pg_catalog.clock_timestamp(),
                    acknowledged_at=pg_catalog.clock_timestamp(),
                    leased_by=NULL,leased_until=NULL,last_error_code=NULL
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='refund'
                  AND e.event_type='finance.refund.completed'
                  AND e.status='processing'
                  AND e.leased_by=p_worker_id
                  AND e.lease_fence=p_lease_fence
                  AND e.leased_until>pg_catalog.clock_timestamp()
                  AND (
                    EXISTS(
                        SELECT 1
                        FROM public.member_entitlement_commands c
                        WHERE c.organization_id=v_org
                          AND c.source_event_id=p_finance_event_id
                          AND c.command_type='recompute_refund'
                    )
                    OR NOT EXISTS(
                        SELECT 1
                        FROM finance.refunds r
                        JOIN finance.payment_allocations a
                          ON a.payment_id=r.payment_id
                        JOIN finance.member_subscription_finance_bindings b
                          ON b.finance_invoice_id=a.invoice_id
                         AND b.organization_id=v_org
                        WHERE r.id=e.aggregate_id
                          AND r.organization_id=v_org
                    )
                  );
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event ack lacks durable command/fence'
                        USING ERRCODE='40001';
                END IF;
                RETURN true;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_release_refund_event(
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
                v_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'worker_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event release requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_error_code IS NULL
                   OR p_error_code !~
                      '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,79}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event release identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT e.attempt_count,e.max_attempts INTO v_event
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='refund'
                  AND e.event_type='finance.refund.completed'
                  AND e.status='processing'
                  AND e.leased_by=p_worker_id
                  AND e.lease_fence=p_lease_fence
                  AND e.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C refund-event release lost fence'
                        USING ERRCODE='40001';
                END IF;

                v_status:=CASE
                    WHEN p_permanent
                      OR v_event.attempt_count>=v_event.max_attempts
                    THEN 'failed' ELSE 'pending'
                END;
                UPDATE finance.outbox_events
                SET status=v_status,leased_by=NULL,leased_until=NULL,
                    last_error_code=p_error_code
                WHERE id=p_finance_event_id;
                RETURN v_status;
            END
            $function$
            """
        )

        for signature in (
            "app_secure.pay24c_claim_refund_events(uuid,integer,integer)",
            "app_secure.pay24c_consume_refund_event(uuid,uuid,bigint)",
            "app_secure.pay24c_ack_refund_event(uuid,uuid,bigint)",
            "app_secure.pay24c_release_refund_event(uuid,uuid,bigint,text,boolean)",
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime")
    finally:
        op.execute("RESET ROLE")


def _install_entitlement_runtime() -> None:
    op.execute("GRANT USAGE ON SCHEMA app_secure TO entitlement_runtime")
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_claim_entitlement_commands(
                p_worker_id uuid,
                p_batch_size integer,
                p_lease_seconds integer
            )
            RETURNS TABLE(
                command_id uuid,
                organization_id uuid,
                command_type text,
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
                    session_user,'entitlement_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C command claim requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL
                   OR p_batch_size IS NULL OR p_batch_size<1 OR p_batch_size>100
                   OR p_lease_seconds IS NULL OR p_lease_seconds<30
                   OR p_lease_seconds>3600
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C command claim arguments invalid'
                        USING ERRCODE='22023';
                END IF;

                PERFORM pg_catalog.set_config(
                    'app.internal_maintenance',
                    'pay24c_entitlement_claim',true
                );

                RETURN QUERY
                WITH candidates AS (
                    SELECT c.command_id,
                           (c.status='processing') AS reclaiming
                    FROM public.member_entitlement_commands c
                    WHERE (
                        c.status='pending'
                        AND c.attempt_count<c.max_attempts
                    ) OR (
                        c.status='processing'
                        AND c.leased_until<=pg_catalog.clock_timestamp()
                    )
                    ORDER BY c.created_at,c.command_id
                    LIMIT p_batch_size
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE public.member_entitlement_commands c
                SET status='processing',
                    attempt_count=CASE
                        WHEN candidate.reclaiming THEN c.attempt_count
                        ELSE c.attempt_count+1
                    END,
                    started_at=COALESCE(
                        c.started_at,pg_catalog.clock_timestamp()
                    ),
                    leased_by=p_worker_id,
                    leased_until=pg_catalog.clock_timestamp()
                        +(p_lease_seconds*INTERVAL '1 second'),
                    lease_fence=c.lease_fence+1,
                    last_error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp()
                FROM candidates candidate
                WHERE c.command_id=candidate.command_id
                RETURNING c.command_id,c.organization_id,c.command_type,
                          c.attempt_count,c.max_attempts,c.lease_fence;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_request_admin_entitlement(
                p_term_id uuid,
                p_command_type text,
                p_idempotency_key text,
                p_reason text,
                p_requested_until date
            )
            RETURNS TABLE(
                command_id uuid,
                command_status text,
                replayed boolean
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_term public.subscription_terms%ROWTYPE;
                v_existing public.member_entitlement_commands%ROWTYPE;
                v_actor uuid;
                v_inserted boolean:=false;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C admin request requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                v_actor:=NULLIF(
                    pg_catalog.current_setting('app.current_user_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_term_id IS NULL
                   OR p_command_type NOT IN (
                       'cancel','freeze','resume','restore','extend'
                   )
                   OR p_idempotency_key IS NULL
                   OR p_idempotency_key !~
                      '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C admin request identity invalid'
                        USING ERRCODE='22023';
                END IF;
                IF p_command_type IN ('cancel','freeze','extend')
                   AND (p_reason IS NULL OR pg_catalog.btrim(p_reason)='')
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C admin request reason required'
                        USING ERRCODE='22023';
                END IF;
                IF p_command_type='extend' AND p_requested_until IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-24-C extension date required'
                        USING ERRCODE='22023';
                END IF;

                SELECT * INTO v_term
                FROM public.subscription_terms t
                WHERE t.id=p_term_id AND t.org_id=v_org;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C admin target term unavailable'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO public.member_entitlement_commands(
                    organization_id,target_term_id,command_type,
                    source_kind,idempotency_key,requested_by,
                    reason,requested_until,payload_json
                ) VALUES (
                    v_org,v_term.id,p_command_type,'admin',
                    p_idempotency_key,v_actor,p_reason,p_requested_until,
                    pg_catalog.jsonb_build_object(
                        'term_version',v_term.version,
                        'requested_status',p_command_type
                    )
                )
                ON CONFLICT (organization_id,idempotency_key) DO NOTHING
                RETURNING * INTO v_existing;

                IF FOUND THEN
                    v_inserted:=true;
                ELSE
                    SELECT * INTO STRICT v_existing
                    FROM public.member_entitlement_commands c
                    WHERE c.organization_id=v_org
                      AND c.idempotency_key=p_idempotency_key;
                    IF v_existing.target_term_id<>p_term_id
                       OR v_existing.command_type<>p_command_type
                       OR v_existing.source_kind<>'admin'
                       OR v_existing.reason IS DISTINCT FROM p_reason
                       OR v_existing.requested_until
                           IS DISTINCT FROM p_requested_until
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C admin idempotency identity conflict'
                            USING ERRCODE='23505';
                    END IF;
                END IF;

                RETURN QUERY SELECT
                    v_existing.command_id,v_existing.status,NOT v_inserted;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_apply_entitlement_command(
                p_command_id uuid,
                p_worker_id uuid,
                p_lease_fence bigint
            )
            RETURNS TABLE(
                command_id uuid,
                subscription_term_id uuid,
                result_status text,
                effect_applied boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_command public.member_entitlement_commands%ROWTYPE;
                v_term public.subscription_terms%ROWTYPE;
                v_apply record;
                v_target text;
                v_business_date date;
                v_event_type subscription_event_type;
                v_event_id uuid;
                v_freeze public.subscription_freezes%ROWTYPE;
                v_days integer;
                v_binding finance.member_subscription_finance_bindings%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;
                v_supported numeric;
                v_payment_count bigint;
                v_legacy uuid;
                v_changed boolean:=false;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'entitlement_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C command apply requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_command_id IS NULL
                   OR p_worker_id IS NULL OR p_lease_fence IS NULL
                   OR p_lease_fence<1
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C command apply identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT * INTO v_command
                FROM public.member_entitlement_commands c
                WHERE c.command_id=p_command_id
                  AND c.organization_id=v_org
                FOR UPDATE;
                IF NOT FOUND
                   OR v_command.status<>'processing'
                   OR v_command.leased_by IS DISTINCT FROM p_worker_id
                   OR v_command.lease_fence IS DISTINCT FROM p_lease_fence
                   OR v_command.leased_until<=pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C command lease/fence lost'
                        USING ERRCODE='40001';
                END IF;

                SELECT * INTO v_term
                FROM public.subscription_terms t
                WHERE t.id=v_command.target_term_id
                  AND t.org_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C command target term unavailable'
                        USING ERRCODE='23514';
                END IF;
                v_legacy:=v_term.legacy_member_subscription_v2_id;

                SELECT COALESCE(
                    pg_catalog.timezone(
                        NULLIF(b.timezone,''),
                        pg_catalog.clock_timestamp()
                    )::date,
                    CURRENT_DATE
                )
                INTO v_business_date
                FROM public.org_branches b
                WHERE b.id=v_term.branch_id AND b.org_id=v_org;
                IF v_business_date IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-24-C command branch timezone unavailable'
                        USING ERRCODE='23514';
                END IF;

                IF v_command.command_type='activate_paid' THEN
                    IF v_command.source_event_id IS NULL
                       OR v_command.source_event_type<>'finance.invoice.paid'
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C paid activation source invalid'
                            USING ERRCODE='23514';
                    END IF;
                    SELECT * INTO v_apply
                    FROM app_secure.apply_member_subscription_finance_event(
                        v_command.source_event_id,
                        'pay24c:apply:'||v_command.command_id::text
                    );
                    v_target:=v_apply.subscription_status;
                    v_changed:=v_apply.activated;

                ELSIF v_command.command_type='recompute_refund' THEN
                    IF v_command.source_event_id IS NULL
                       OR v_command.source_event_type<>'finance.refund.completed'
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C refund recomputation source invalid'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT * INTO v_binding
                    FROM finance.member_subscription_finance_bindings b
                    WHERE b.organization_id=v_org
                      AND b.subscription_term_id=v_term.id;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-C refund recomputation binding missing'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT * INTO v_invoice
                    FROM finance.invoices i
                    WHERE i.id=v_binding.finance_invoice_id
                      AND i.organization_id=v_org;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-C refund recomputation invoice missing'
                            USING ERRCODE='23514';
                    END IF;

                    IF EXISTS(
                        SELECT 1
                        FROM finance.payment_allocations target_allocation
                        JOIN finance.refunds refunded
                          ON refunded.payment_id=target_allocation.payment_id
                         AND refunded.organization_id=v_org
                         AND refunded.status='succeeded'
                        WHERE target_allocation.invoice_id=v_invoice.id
                          AND EXISTS(
                              SELECT 1
                              FROM finance.payment_allocations other_allocation
                              WHERE other_allocation.payment_id=
                                    target_allocation.payment_id
                                AND other_allocation.invoice_id<>v_invoice.id
                          )
                    ) THEN
                        UPDATE public.member_entitlement_commands
                        SET status='review_required',
                            result_status=v_term.status::text,
                            completed_at=pg_catalog.clock_timestamp(),
                            leased_by=NULL,leased_until=NULL,
                            last_error_code='ambiguous_refund_allocation',
                            updated_at=pg_catalog.clock_timestamp()
                        WHERE command_id=v_command.command_id
                          AND organization_id=v_org;
                        RETURN QUERY SELECT
                            v_command.command_id,v_term.id,
                            v_term.status::text,false,false;
                        RETURN;
                    END IF;

                    SELECT count(DISTINCT a.payment_id),
                           COALESCE(sum(
                               LEAST(
                                   a.allocated_amount,
                                   GREATEST(
                                       p.amount-COALESCE(
                                           (
                                               SELECT sum(r.amount)
                                               FROM finance.refunds r
                                               WHERE r.payment_id=p.id
                                                 AND r.organization_id=v_org
                                                 AND r.status='succeeded'
                                           ),0
                                       ),
                                       0
                                   )
                               )
                           ),0)
                    INTO v_payment_count,v_supported
                    FROM finance.payment_allocations a
                    JOIN finance.payments p ON p.id=a.payment_id
                    WHERE a.invoice_id=v_invoice.id;

                    IF v_payment_count=0 THEN
                        RAISE EXCEPTION
                            'PAY-24-C refund recomputation has no allocations'
                            USING ERRCODE='23514';
                    END IF;

                    IF v_supported>=v_binding.amount THEN
                        v_target:=v_term.status::text;
                    ELSIF v_term.status::text='scheduled' THEN
                        UPDATE public.subscription_terms
                        SET status='voided',
                            voided_at=pg_catalog.clock_timestamp(),
                            void_reason='PAY24C_REFUND_UNDERFUNDED',
                            updated_at=pg_catalog.clock_timestamp(),
                            version=version+1
                        WHERE id=v_term.id AND org_id=v_org;
                        v_target:='voided';
                        v_event_type:='term_voided';
                        v_changed:=true;
                    ELSIF v_term.status::text='active'
                          AND v_business_date<=v_term.effective_ends_on THEN
                        UPDATE public.subscription_terms
                        SET status='terminated',
                            terminated_at=pg_catalog.clock_timestamp(),
                            termination_reason='PAY24C_REFUND_UNDERFUNDED',
                            updated_at=pg_catalog.clock_timestamp(),
                            version=version+1
                        WHERE id=v_term.id AND org_id=v_org;
                        v_target:='terminated';
                        v_event_type:='term_terminated';
                        v_changed:=true;
                    ELSE
                        v_target:=CASE
                            WHEN v_business_date>v_term.effective_ends_on
                            THEN 'expired'
                            ELSE v_term.status::text
                        END;
                    END IF;

                    IF v_changed AND v_legacy IS NOT NULL THEN
                        UPDATE public.member_subscriptions_v2
                        SET status='cancelled',
                            cancelled_at=pg_catalog.clock_timestamp(),
                            updated_at=pg_catalog.clock_timestamp()
                        WHERE id=v_legacy AND org_id=v_org;
                    END IF;

                ELSIF v_command.command_type='cancel' THEN
                    IF v_term.status::text IN (
                        'pending_payment','scheduled','active'
                    ) THEN
                        UPDATE public.subscription_terms
                        SET status='cancelled',
                            cancelled_at=pg_catalog.clock_timestamp(),
                            cancelled_by=v_command.requested_by,
                            cancellation_reason=v_command.reason,
                            updated_at=pg_catalog.clock_timestamp(),
                            version=version+1
                        WHERE id=v_term.id AND org_id=v_org;
                        v_target:='cancelled';
                        v_event_type:='term_cancelled';
                        v_changed:=true;
                        IF v_legacy IS NOT NULL THEN
                            UPDATE public.member_subscriptions_v2
                            SET status='cancelled',
                                cancelled_at=pg_catalog.clock_timestamp(),
                                updated_at=pg_catalog.clock_timestamp(),
                                updated_by=v_command.requested_by
                            WHERE id=v_legacy AND org_id=v_org;
                        END IF;
                    ELSIF v_term.status::text='cancelled' THEN
                        v_target:='cancelled';
                    ELSE
                        RAISE EXCEPTION
                            'PAY-24-C cancellation transition rejected: %',
                            v_term.status::text
                            USING ERRCODE='23514';
                    END IF;

                ELSIF v_command.command_type='freeze' THEN
                    IF v_term.status::text<>'active'
                       OR v_business_date<v_term.starts_on
                       OR v_business_date>v_term.effective_ends_on
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C freeze requires active entitlement window'
                            USING ERRCODE='23514';
                    END IF;
                    IF EXISTS(
                        SELECT 1 FROM public.subscription_freezes f
                        WHERE f.term_id=v_term.id AND f.org_id=v_org
                          AND f.status='active'
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-C term already frozen'
                            USING ERRCODE='23505';
                    END IF;
                    INSERT INTO public.subscription_freezes(
                        id,org_id,series_id,term_id,status,
                        requested_starts_on,planned_ends_on,
                        extension_days,extension_policy,reason,
                        requested_at,requested_by,approved_at,approved_by
                    ) VALUES (
                        pg_catalog.gen_random_uuid(),v_org,v_term.series_id,
                        v_term.id,'active',v_business_date,
                        v_command.requested_until,0,'extend_expiry',
                        v_command.reason,pg_catalog.clock_timestamp(),
                        v_command.requested_by,pg_catalog.clock_timestamp(),
                        v_command.requested_by
                    ) RETURNING * INTO v_freeze;
                    v_target:='frozen';
                    v_event_type:='freeze_started';
                    v_changed:=true;
                    IF v_legacy IS NOT NULL THEN
                        UPDATE public.member_subscriptions_v2
                        SET status='frozen',updated_at=pg_catalog.clock_timestamp(),
                            updated_by=v_command.requested_by
                        WHERE id=v_legacy AND org_id=v_org;
                    END IF;

                ELSIF v_command.command_type='resume' THEN
                    SELECT * INTO v_freeze
                    FROM public.subscription_freezes f
                    WHERE f.term_id=v_term.id AND f.org_id=v_org
                      AND f.status='active'
                    ORDER BY f.requested_at DESC
                    LIMIT 1
                    FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-C resume requires active freeze'
                            USING ERRCODE='23514';
                    END IF;
                    v_days:=GREATEST(
                        v_business_date-v_freeze.requested_starts_on,0
                    );
                    UPDATE public.subscription_freezes
                    SET status='completed',
                        actual_ended_on=v_business_date,
                        extension_days=v_days,
                        resumed_at=pg_catalog.clock_timestamp(),
                        resumed_by=v_command.requested_by,
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_freeze.id;
                    IF v_freeze.extension_policy='extend_expiry' THEN
                        UPDATE public.subscription_terms
                        SET effective_ends_on=effective_ends_on+v_days,
                            updated_at=pg_catalog.clock_timestamp(),
                            version=version+1
                        WHERE id=v_term.id AND org_id=v_org;
                    END IF;
                    SELECT * INTO v_term
                    FROM public.subscription_terms
                    WHERE id=v_term.id AND org_id=v_org;
                    v_target:=CASE
                        WHEN v_business_date>v_term.effective_ends_on
                        THEN 'expired' ELSE 'active'
                    END;
                    v_event_type:='freeze_resumed';
                    v_changed:=true;
                    IF v_legacy IS NOT NULL THEN
                        UPDATE public.member_subscriptions_v2
                        SET status=CASE
                                WHEN v_target='active' THEN 'active'
                                ELSE 'expired'
                            END::modern_subscription_status,
                            end_date=v_term.effective_ends_on,
                            updated_at=pg_catalog.clock_timestamp(),
                            updated_by=v_command.requested_by
                        WHERE id=v_legacy AND org_id=v_org;
                    END IF;

                ELSIF v_command.command_type='extend' THEN
                    IF v_term.status::text NOT IN ('scheduled','active')
                       OR v_command.requested_until IS NULL
                       OR v_command.requested_until<=v_term.effective_ends_on
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C extension transition rejected'
                            USING ERRCODE='23514';
                    END IF;
                    UPDATE public.subscription_terms
                    SET effective_ends_on=v_command.requested_until,
                        updated_at=pg_catalog.clock_timestamp(),
                        version=version+1
                    WHERE id=v_term.id AND org_id=v_org;
                    v_target:=v_term.status::text;
                    v_changed:=true;
                    IF v_legacy IS NOT NULL THEN
                        UPDATE public.member_subscriptions_v2
                        SET end_date=v_command.requested_until,
                            updated_at=pg_catalog.clock_timestamp(),
                            updated_by=v_command.requested_by
                        WHERE id=v_legacy AND org_id=v_org;
                    END IF;

                ELSIF v_command.command_type='restore' THEN
                    IF v_term.status::text NOT IN (
                        'cancelled','terminated','voided'
                    ) OR v_business_date>v_term.effective_ends_on
                    THEN
                        RAISE EXCEPTION
                            'PAY-24-C restore transition rejected'
                            USING ERRCODE='23514';
                    END IF;
                    SELECT * INTO v_binding
                    FROM finance.member_subscription_finance_bindings b
                    WHERE b.organization_id=v_org
                      AND b.subscription_term_id=v_term.id;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-C restore requires Finance binding'
                            USING ERRCODE='23514';
                    END IF;
                    SELECT * INTO v_invoice
                    FROM finance.invoices i
                    WHERE i.id=v_binding.finance_invoice_id
                      AND i.organization_id=v_org
                      AND i.status='paid';
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'PAY-24-C restore requires paid invoice'
                            USING ERRCODE='23514';
                    END IF;
                    IF EXISTS(
                        SELECT 1
                        FROM finance.payment_allocations target_allocation
                        JOIN finance.refunds refunded
                          ON refunded.payment_id=target_allocation.payment_id
                         AND refunded.organization_id=v_org
                         AND refunded.status='succeeded'
                        WHERE target_allocation.invoice_id=v_invoice.id
                          AND EXISTS(
                              SELECT 1
                              FROM finance.payment_allocations other_allocation
                              WHERE other_allocation.payment_id=
                                    target_allocation.payment_id
                                AND other_allocation.invoice_id<>v_invoice.id
                          )
                    ) THEN
                        RAISE EXCEPTION
                            'PAY-24-C restore refund allocation is ambiguous'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT count(DISTINCT a.payment_id),
                           COALESCE(sum(
                               LEAST(
                                   a.allocated_amount,
                                   GREATEST(
                                       p.amount-COALESCE(
                                           (
                                               SELECT sum(r.amount)
                                               FROM finance.refunds r
                                               WHERE r.payment_id=p.id
                                                 AND r.organization_id=v_org
                                                 AND r.status='succeeded'
                                           ),0
                                       ),0
                                   )
                               )
                           ),0)
                    INTO v_payment_count,v_supported
                    FROM finance.payment_allocations a
                    JOIN finance.payments p ON p.id=a.payment_id
                    WHERE a.invoice_id=v_invoice.id;
                    IF v_payment_count=0 OR v_supported<v_binding.amount THEN
                        RAISE EXCEPTION
                            'PAY-24-C restore lacks full net financial support'
                            USING ERRCODE='23514';
                    END IF;
                    v_target:=CASE
                        WHEN v_business_date<v_term.starts_on
                        THEN 'scheduled' ELSE 'active'
                    END;
                    UPDATE public.subscription_terms
                    SET status=v_target::subscription_term_status,
                        activated_at=CASE WHEN v_target='active'
                            THEN pg_catalog.clock_timestamp() ELSE NULL END,
                        cancelled_at=NULL,cancelled_by=NULL,
                        cancellation_reason=NULL,
                        terminated_at=NULL,terminated_by=NULL,
                        termination_reason=NULL,
                        voided_at=NULL,voided_by=NULL,void_reason=NULL,
                        updated_at=pg_catalog.clock_timestamp(),
                        version=version+1
                    WHERE id=v_term.id AND org_id=v_org;
                    v_event_type:=CASE WHEN v_target='active'
                        THEN 'term_activated'::subscription_event_type
                        ELSE 'term_scheduled'::subscription_event_type END;
                    v_changed:=true;
                    IF v_legacy IS NOT NULL THEN
                        UPDATE public.member_subscriptions_v2
                        SET status=CASE WHEN v_target='active'
                                THEN 'active' ELSE 'pending'
                            END::modern_subscription_status,
                            cancelled_at=NULL,
                            updated_at=pg_catalog.clock_timestamp(),
                            updated_by=v_command.requested_by
                        WHERE id=v_legacy AND org_id=v_org;
                    END IF;
                ELSE
                    RAISE EXCEPTION
                        'PAY-24-C unsupported command type'
                        USING ERRCODE='22023';
                END IF;

                IF v_changed AND v_event_type IS NOT NULL THEN
                    v_event_id:=pg_catalog.gen_random_uuid();
                    INSERT INTO public.subscription_events(
                        id,org_id,branch_id,series_id,term_id,event_type,
                        event_at,actor_user_id,event_source,correlation_id,
                        idempotency_key,metadata,before_snapshot,after_snapshot
                    ) VALUES (
                        v_event_id,v_org,v_term.branch_id,v_term.series_id,
                        v_term.id,v_event_type,pg_catalog.clock_timestamp(),
                        v_command.requested_by,'pay24c',
                        v_command.command_id::text,
                        'pay24c:event:'||v_command.command_id::text,
                        pg_catalog.jsonb_build_object(
                            'command_id',v_command.command_id::text,
                            'command_type',v_command.command_type,
                            'source_kind',v_command.source_kind,
                            'source_event_id',v_command.source_event_id
                        ),
                        pg_catalog.jsonb_build_object(
                            'status',v_term.status::text
                        ),
                        pg_catalog.jsonb_build_object(
                            'status',v_target
                        )
                    );
                END IF;

                UPDATE public.member_entitlement_commands c
                SET status='succeeded',
                    result_status=v_target,
                    result_event_id=v_event_id,
                    completed_at=pg_catalog.clock_timestamp(),
                    leased_by=NULL,leased_until=NULL,
                    last_error_code=NULL,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE c.command_id=v_command.command_id
                  AND c.organization_id=v_org;

                RETURN QUERY SELECT
                    v_command.command_id,v_term.id,v_target,
                    v_changed,false;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24c_release_entitlement_command(
                p_command_id uuid,
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
                v_command public.member_entitlement_commands%ROWTYPE;
                v_status text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'entitlement_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C command release requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_org IS NULL OR p_error_code IS NULL
                   OR p_error_code !~
                      '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,79}$'
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C command release identity invalid'
                        USING ERRCODE='22023';
                END IF;
                SELECT * INTO v_command
                FROM public.member_entitlement_commands c
                WHERE c.command_id=p_command_id
                  AND c.organization_id=v_org
                  AND c.status='processing'
                  AND c.leased_by=p_worker_id
                  AND c.lease_fence=p_lease_fence
                  AND c.leased_until>pg_catalog.clock_timestamp()
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'PAY-24-C command release lost lease/fence'
                        USING ERRCODE='40001';
                END IF;
                v_status:=CASE
                    WHEN p_permanent
                      OR v_command.attempt_count>=v_command.max_attempts
                    THEN 'failed' ELSE 'pending'
                END;
                UPDATE public.member_entitlement_commands
                SET status=v_status,leased_by=NULL,leased_until=NULL,
                    last_error_code=p_error_code,
                    completed_at=CASE WHEN v_status='failed'
                        THEN pg_catalog.clock_timestamp() ELSE NULL END,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=p_command_id;
                RETURN v_status;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.member_entitlement_access_active(
                p_org_id uuid,
                p_member_id uuid
            )
            RETURNS boolean
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public
            SET row_security=on
            AS $function$
            DECLARE
                v_context_org uuid;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'app_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-C member access requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_org_id IS NULL OR p_member_id IS NULL THEN
                    RETURN false;
                END IF;
                v_context_org:=NULLIF(
                    pg_catalog.current_setting('app.current_org_id',true),''
                )::uuid;
                IF v_context_org IS NOT NULL
                   AND v_context_org<>p_org_id
                THEN
                    RAISE EXCEPTION
                        'PAY-24-C member access tenant mismatch'
                        USING ERRCODE='42501';
                END IF;
                PERFORM pg_catalog.set_config(
                    'app.current_org_id',p_org_id::text,true
                );

                RETURN EXISTS(
                    SELECT 1
                    FROM public.subscription_slot_assignments a
                    JOIN public.subscription_terms t
                      ON t.id=a.term_id AND t.org_id=a.org_id
                    JOIN public.org_branches b
                      ON b.id=t.branch_id AND b.org_id=t.org_id
                    WHERE a.org_id=p_org_id
                      AND a.member_id=p_member_id
                      AND a.assignment_state='active'
                      AND a.effective_from<=COALESCE(
                          pg_catalog.timezone(
                              NULLIF(b.timezone,''),
                              pg_catalog.clock_timestamp()
                          )::date,
                          CURRENT_DATE
                      )
                      AND (
                          a.effective_until IS NULL
                          OR a.effective_until>=COALESCE(
                              pg_catalog.timezone(
                                  NULLIF(b.timezone,''),
                                  pg_catalog.clock_timestamp()
                              )::date,
                              CURRENT_DATE
                          )
                      )
                      AND t.status IN ('scheduled','active')
                      AND t.starts_on<=COALESCE(
                          pg_catalog.timezone(
                              NULLIF(b.timezone,''),
                              pg_catalog.clock_timestamp()
                          )::date,
                          CURRENT_DATE
                      )
                      AND t.effective_ends_on>=COALESCE(
                          pg_catalog.timezone(
                              NULLIF(b.timezone,''),
                              pg_catalog.clock_timestamp()
                          )::date,
                          CURRENT_DATE
                      )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM public.subscription_freezes f
                          WHERE f.org_id=t.org_id
                            AND f.term_id=t.id
                            AND f.status='active'
                            AND f.requested_starts_on<=COALESCE(
                                pg_catalog.timezone(
                                    NULLIF(b.timezone,''),
                                    pg_catalog.clock_timestamp()
                                )::date,
                                CURRENT_DATE
                            )
                            AND (
                                f.planned_ends_on IS NULL
                                OR f.planned_ends_on>=COALESCE(
                                    pg_catalog.timezone(
                                        NULLIF(b.timezone,''),
                                        pg_catalog.clock_timestamp()
                                    )::date,
                                    CURRENT_DATE
                                )
                            )
                      )
                );
            END
            $function$
            """
        )

        for signature in (
            "app_secure.pay24c_claim_entitlement_commands(uuid,integer,integer)",
            "app_secure.pay24c_apply_entitlement_command(uuid,uuid,bigint)",
            "app_secure.pay24c_release_entitlement_command(uuid,uuid,bigint,text,boolean)",
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO entitlement_runtime"
            )

        for signature in (
            "app_secure.pay24c_request_admin_entitlement(uuid,text,text,text,date)",
            "app_secure.member_entitlement_access_active(uuid,uuid)",
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO app_runtime"
            )
    finally:
        op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    # Runtime roles may execute only their exact PAY-24-C interface and must
    # never receive direct DML on protected entitlement tables.
    protected = (
        "public.subscription_terms",
        "public.subscription_freezes",
        "public.subscription_events",
        "public.member_entitlement_commands",
    )
    for role in _RUNTIME_ROLES:
        for relation in protected:
            direct = bool(
                bind.execute(
                    sa.text(
                        "SELECT "
                        "pg_catalog.has_table_privilege(:role,:relation,'INSERT') "
                        "OR pg_catalog.has_table_privilege(:role,:relation,'UPDATE') "
                        "OR pg_catalog.has_table_privilege(:role,:relation,'DELETE') "
                        "OR pg_catalog.has_table_privilege(:role,:relation,'TRUNCATE')"
                    ),
                    {"role": role, "relation": relation},
                ).scalar_one()
            )
            if direct:
                raise RuntimeError(
                    f"PAY24-C leaked direct entitlement DML: {role}->{relation}"
                )

    if any(
        bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    "'app_security_owner',:relation,'TRIGGER')"
                ),
                {"relation": relation},
            ).scalar_one()
        )
        for relation in (
            "public.subscription_terms",
            "public.member_subscriptions_v2",
            "public.subscription_freezes",
        )
    ):
        raise RuntimeError(
            "PAY24-C temporary app_security_owner TRIGGER grant leaked"
        )

    if bool(
        bind.execute(
            sa.text(
                "SELECT "
                "pg_catalog.has_table_privilege("
                "'app_user','public.member_subscriptions','UPDATE') "
                "OR pg_catalog.has_table_privilege("
                "'app_runtime','public.member_subscriptions','UPDATE') "
                "OR pg_catalog.has_table_privilege("
                "'app_user','public.member_subscriptions_v2','UPDATE')"
            )
        ).scalar_one()
    ):
        raise RuntimeError("PAY24-C legacy subscription mutation ACL remains")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        expected = {
            "app_secure.pay24c_claim_entitlement_commands(uuid,integer,integer)":
                "entitlement_runtime",
            "app_secure.pay24c_apply_entitlement_command(uuid,uuid,bigint)":
                "entitlement_runtime",
            "app_secure.pay24c_release_entitlement_command(uuid,uuid,bigint,text,boolean)":
                "entitlement_runtime",
            "app_secure.pay24c_enqueue_paid_activation(uuid,text)":
                "worker_runtime",
            "app_secure.pay24c_claim_refund_events(uuid,integer,integer)":
                "worker_runtime",
            "app_secure.pay24c_consume_refund_event(uuid,uuid,bigint)":
                "worker_runtime",
            "app_secure.pay24c_ack_refund_event(uuid,uuid,bigint)":
                "worker_runtime",
            "app_secure.pay24c_release_refund_event(uuid,uuid,bigint,text,boolean)":
                "worker_runtime",
            "app_secure.pay24c_request_admin_entitlement(uuid,text,text,text,date)":
                "app_runtime",
            "app_secure.member_entitlement_access_active(uuid,uuid)":
                "app_runtime",
        }
        for signature, allowed in expected.items():
            oid = bind.execute(
                sa.text(
                    "SELECT pg_catalog.to_regprocedure(:signature)::oid"
                ),
                {"signature": signature},
            ).scalar_one()
            for role in _RUNTIME_ROLES:
                has_execute = bool(
                    bind.execute(
                        sa.text(
                            "SELECT pg_catalog.has_function_privilege("
                            ":role,CAST(:oid AS oid),'EXECUTE')"
                        ),
                        {"role": role, "oid": oid},
                    ).scalar_one()
                )
                if has_execute is not (role == allowed):
                    raise RuntimeError(
                        "PAY24-C function ACL drift: "
                        f"{signature} role={role} expected={allowed}"
                    )
    finally:
        op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    _require_prerequisites(bind)
    _create_snapshots(bind)
    _revoke_legacy_direct_writes()
    _install_command_table()
    _replace_predecessor_authority(bind)
    _install_guards()
    _install_enqueue_and_pay5_successor()
    _install_refund_enqueue()
    _install_entitlement_runtime()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='60s'")

    # Fail closed once any durable entitlement command/effect exists.
    op.execute(
        "ALTER TABLE public.member_entitlement_commands "
        "NO FORCE ROW LEVEL SECURITY"
    )
    if bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS("
                "SELECT 1 FROM public.member_entitlement_commands LIMIT 1)"
            )
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY24-C downgrade blocked: entitlement command history exists"
        )

    for trigger, table in (
        ("trg_pay24c_freeze_mutation_guard", "public.subscription_freezes"),
        ("trg_pay24c_v2_mutation_guard", "public.member_subscriptions_v2"),
        ("trg_pay24c_term_mutation_guard", "public.subscription_terms"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (
            "app_secure.member_entitlement_access_active(uuid,uuid)",
            "app_secure.pay24c_request_admin_entitlement(uuid,text,text,text,date)",
            "app_secure.pay24c_release_entitlement_command(uuid,uuid,bigint,text,boolean)",
            "app_secure.pay24c_apply_entitlement_command(uuid,uuid,bigint)",
            "app_secure.pay24c_claim_entitlement_commands(uuid,integer,integer)",
            "app_secure.pay24c_release_refund_event(uuid,uuid,bigint,text,boolean)",
            "app_secure.pay24c_ack_refund_event(uuid,uuid,bigint)",
            "app_secure.pay24c_consume_refund_event(uuid,uuid,bigint)",
            "app_secure.pay24c_claim_refund_events(uuid,integer,integer)",
            "app_secure.pay24c_enqueue_paid_activation(uuid,text)",
            "app_secure.pay24c_guard_freeze_mutation()",
            "app_secure.pay24c_guard_v2_mutation()",
            "app_secure.pay24c_guard_term_mutation()",
        ):
            op.execute(f"DROP FUNCTION {signature}")

        definitions = bind.execute(
            sa.text(
                """
                SELECT signature,definition
                FROM app_private.pay24c_predecessor_functions
                ORDER BY signature
                """
            )
        ).all()
        for signature, definition in definitions:
            op.execute(str(definition))
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY pay24c_subscription_freezes_security_owner "
        "ON public.subscription_freezes"
    )
    op.execute(
        "DROP POLICY pay24c_commands_security_owner "
        "ON public.member_entitlement_commands"
    )
    op.execute("DROP TABLE public.member_entitlement_commands RESTRICT")
    op.execute("REVOKE USAGE ON SCHEMA app_secure FROM entitlement_runtime")

    rows = bind.execute(
        sa.text(
            """
            SELECT grantee,relation_name,privilege_name,was_present
            FROM app_private.pay24c_acl_snapshot
            ORDER BY grantee,relation_name,privilege_name
            """
        )
    ).all()
    for grantee, relation, privilege, was_present in rows:
        if bool(was_present):
            op.execute(
                f"GRANT {privilege} ON TABLE {relation} TO {grantee}"
            )

    op.execute("DROP TABLE app_private.pay24c_acl_snapshot")
    op.execute("DROP TABLE app_private.pay24c_predecessor_functions")
