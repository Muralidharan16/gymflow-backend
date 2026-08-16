"""Add durable, evidence-backed P4C notification delivery boundary.

Revision ID: w07d8e9f0a37
Revises: v07d8e9f0a36
Create Date: 2026-08-16

P4C turns lifecycle member-notification intent into tenant-bound per-recipient
commands. Queue payloads never authorize destinations. A leased worker must use
SECURITY DEFINER capabilities that re-read current member/contact/preferences,
and provider acceptance stays distinct from terminal delivery evidence.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "w07d8e9f0a37"
down_revision = "v07d8e9f0a36"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_APP = "app_runtime"

_COMMANDS = "public.notification_commands"
_ATTEMPTS = "public.notification_delivery_attempts"
_WEBHOOKS = "public.notification_provider_events"
_PREFS = "public.member_notification_preferences"

_OUTBOX_STATUS_CONSTRAINT = "branch_outbox_events_status_check"
_PREDECESSOR_OUTBOX_STATUSES = (
    "pending",
    "processing",
    "delivered",
    "dead_lettered",
    "quarantined",
    "compatibility_queue",
    "superseded",
)
_P4C_OUTBOX_STATUSES = (*_PREDECESSOR_OUTBOX_STATUSES, "provider_accepted")

_FUNCTIONS = (
    "app_secure.materialize_branch_member_notifications(uuid,uuid)",
    "app_secure.claim_notification_delivery(uuid,uuid)",
    "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,text,text,text,text)",
    "app_secure.record_notification_delivery_failure(uuid,uuid,text,text,text,text)",
    "app_secure.apply_notification_provider_event(text,text,text,text,timestamptz,text)",
    "app_secure.enqueue_notification_reconciliation(integer)",
    "app_secure.claim_notification_reconciliation(uuid,uuid)",
    "app_secure.apply_notification_reconciliation_result(uuid,uuid,uuid,text,text)",
)


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("w07 P4C migration requires migration_owner")
    if any(bool(row[k]) for k in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole",
        "rolreplication", "rolbypassrls",
    )):
        raise RuntimeError("w07 migration_owner violates reduced role contract")

    for role_name in (_SECURITY_OWNER, _WORKER, _MAINTENANCE, _APP, "auth_runtime"):
        role = bind.execute(
            sa.text(
                """
                SELECT rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                       rolreplication, rolbypassrls
                FROM pg_catalog.pg_roles WHERE rolname=:role_name
                """
            ),
            {"role_name": role_name},
        ).mappings().one_or_none()
        if role is None or any(bool(role[k]) for k in role):
            raise RuntimeError(f"w07 reduced role contract drift: {role_name}")
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": role_name, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"w07 runtime may SET ROLE app_security_owner: {role_name}")


def _require_predecessor(bind) -> None:
    for relation in (
        "public.branch_outbox_events",
        "public.branch_status_history",
        "public.org_branches",
        "public.members",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"w07 missing predecessor relation {relation}")
    for relation in (_COMMANDS, _ATTEMPTS, _WEBHOOKS, _PREFS):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"w07 relation collision: {relation}")

    definition = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(c.oid,true)
            FROM pg_catalog.pg_constraint c
            WHERE c.conrelid='public.branch_outbox_events'::regclass
              AND c.conname=:name AND c.contype='c'
            """
        ),
        {"name": _OUTBOX_STATUS_CONSTRAINT},
    ).scalar_one_or_none()
    if definition is None:
        raise RuntimeError("w07 predecessor outbox status CHECK missing")
    for status in _PREDECESSOR_OUTBOX_STATUSES:
        if f"'{status}'" not in definition:
            raise RuntimeError(f"w07 predecessor outbox status missing {status}")
    if "'provider_accepted'" in definition:
        raise RuntimeError("w07 predecessor unexpectedly admits provider_accepted")


def _install_outbox_status_contract(statuses: tuple[str, ...]) -> None:
    rendered = ", ".join(f"'{item}'" for item in statuses)
    op.execute(
        f"""
        ALTER TABLE public.branch_outbox_events
          DROP CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT},
          ADD CONSTRAINT {_OUTBOX_STATUS_CONSTRAINT}
          CHECK (status IN ({rendered}))
        """
    )


def _create_tables() -> None:
    op.execute(
        """
        CREATE TABLE public.member_notification_preferences (
            tenant_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
            member_id uuid NOT NULL REFERENCES public.members(id) ON DELETE CASCADE,
            email_enabled boolean NOT NULL DEFAULT true,
            whatsapp_enabled boolean NOT NULL DEFAULT false,
            email_suppressed_at timestamptz,
            whatsapp_suppressed_at timestamptz,
            suppression_reason text,
            updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, member_id)
        );

        CREATE TABLE public.notification_commands (
            command_id uuid PRIMARY KEY,
            source_outbox_id uuid NOT NULL REFERENCES public.branch_outbox_events(outbox_id) ON DELETE RESTRICT,
            tenant_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            branch_id uuid NOT NULL REFERENCES public.org_branches(id) ON DELETE RESTRICT,
            member_id uuid NOT NULL REFERENCES public.members(id) ON DELETE RESTRICT,
            effect_type text NOT NULL,
            channel text NOT NULL CHECK (channel IN ('email','whatsapp')),
            template_key text NOT NULL,
            template_data jsonb NOT NULL,
            idempotency_key text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending','processing','provider_accepted','succeeded',
                'retry_pending','dead_lettered','cancelled','superseded'
            )),
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5,
            next_attempt_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            leased_by uuid,
            leased_until timestamptz,
            provider_code text,
            provider_reference_id text,
            provider_event_id text,
            request_sha256 text,
            acknowledgement_evidence_sha256 text,
            destination_sha256 text,
            delivery_outcome text,
            attempted_at timestamptz,
            acknowledged_at timestamptz,
            completed_at timestamptz,
            last_provider_event_at timestamptz,
            last_error text,
            dead_letter_reason text,
            correlation_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT notification_commands_attempt_bounds
              CHECK (max_attempts BETWEEN 1 AND 20 AND attempt_count BETWEEN 0 AND max_attempts),
            CONSTRAINT notification_commands_lease_state
              CHECK ((status='processing') = (leased_by IS NOT NULL AND leased_until IS NOT NULL)),
            CONSTRAINT notification_commands_hash_shape
              CHECK (
                (request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$') AND
                (acknowledgement_evidence_sha256 IS NULL OR acknowledgement_evidence_sha256 ~ '^[0-9a-f]{64}$') AND
                (destination_sha256 IS NULL OR destination_sha256 ~ '^[0-9a-f]{64}$')
              )
        );
        CREATE UNIQUE INDEX uq_notification_provider_reference
          ON public.notification_commands(provider_code, provider_reference_id)
          WHERE provider_reference_id IS NOT NULL;
        CREATE INDEX ix_notification_due
          ON public.notification_commands(status, next_attempt_at, created_at);
        CREATE INDEX ix_notification_tenant_member
          ON public.notification_commands(tenant_id, member_id, created_at DESC);
        CREATE INDEX ix_notification_source
          ON public.notification_commands(source_outbox_id, command_id);

        CREATE TABLE public.notification_delivery_attempts (
            attempt_id uuid PRIMARY KEY,
            command_id uuid NOT NULL REFERENCES public.notification_commands(command_id) ON DELETE RESTRICT,
            tenant_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 20),
            outcome text NOT NULL CHECK (outcome IN (
                'definite_success','provider_accepted_nonterminal',
                'permanent_rejection','retryable_failure','ambiguous_outcome'
            )),
            provider_code text NOT NULL,
            provider_reference_id text,
            request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            provider_evidence_sha256 text,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            UNIQUE (command_id, attempt_number),
            CHECK (provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$')
        );

        CREATE TABLE public.notification_provider_events (
            provider_code text NOT NULL,
            provider_event_id text NOT NULL,
            provider_reference_id text NOT NULL,
            command_id uuid REFERENCES public.notification_commands(command_id) ON DELETE RESTRICT,
            event_type text NOT NULL,
            event_created_at timestamptz NOT NULL,
            evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            received_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (provider_code, provider_event_id)
        );
        CREATE INDEX ix_notification_provider_event_reference
          ON public.notification_provider_events(provider_code, provider_reference_id, event_created_at DESC);
        """
    )

    for table in (_PREFS, _COMMANDS, _ATTEMPTS, _WEBHOOKS):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        table_name = table.split(".", 1)[1]
        op.execute(
            f"""
            CREATE POLICY {table_name}_security_owner_policy ON {table}
              FOR ALL TO app_security_owner USING (true) WITH CHECK (true)
            """
        )
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON TABLE {table} FROM app_runtime, auth_runtime, worker_runtime, lifecycle_maintenance_runtime")

    # Only the NOLOGIN security owner receives the direct data surface required
    # by SECURITY DEFINER capabilities. Runtime identities receive functions only.
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.member_notification_preferences TO app_security_owner")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.notification_commands TO app_security_owner")
    op.execute("GRANT SELECT, INSERT ON TABLE public.notification_delivery_attempts TO app_security_owner")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.notification_provider_events TO app_security_owner")

    # Bounded source projection needed for authoritative fanout/claim.
    op.execute("GRANT SELECT (history_id,branch_id,from_status,to_status,correlation_id,changed_at) ON public.branch_status_history TO app_security_owner")
    op.execute("GRANT SELECT (id,org_id,home_branch_id,name,email,phone,status,is_active) ON public.members TO app_security_owner")


def _create_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")

    op.execute(
        """
        CREATE FUNCTION app_secure.materialize_branch_member_notifications(
            p_outbox_id uuid,
            p_worker_id uuid
        ) RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE
            v_tenant uuid;
            v_branch uuid;
            v_correlation uuid;
            v_from text;
            v_to text;
            v_branch_name text;
            v_count integer;
        BEGIN
            IF p_outbox_id IS NULL OR p_worker_id IS NULL THEN
                RAISE EXCEPTION 'notification fanout requires outbox and worker ids' USING ERRCODE='22023';
            END IF;

            SELECT o.tenant_id,o.branch_id,o.correlation_id
              INTO v_tenant,v_branch,v_correlation
            FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id
              AND o.event_type='branch.member_notification'
              AND o.status='processing'
              AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification fanout requires a live owned lease' USING ERRCODE='42501';
            END IF;

            SELECT h.from_status,h.to_status
              INTO v_from,v_to
            FROM public.branch_status_history h
            WHERE h.branch_id=v_branch AND h.correlation_id=v_correlation
            ORDER BY h.changed_at DESC LIMIT 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification fanout lifecycle history missing' USING ERRCODE='P0002';
            END IF;

            SELECT b.branch_name INTO v_branch_name
            FROM public.org_branches b
            WHERE b.id=v_branch AND b.org_id=v_tenant;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification fanout branch missing' USING ERRCODE='P0002';
            END IF;

            INSERT INTO public.notification_commands(
                command_id,source_outbox_id,tenant_id,branch_id,member_id,effect_type,
                channel,template_key,template_data,idempotency_key,status,attempt_count,
                max_attempts,next_attempt_at,correlation_id,created_at,updated_at
            )
            SELECT
                pg_catalog.gen_random_uuid(),p_outbox_id,v_tenant,v_branch,m.id,
                'branch.member_notification','email','branch_lifecycle_status_changed',
                pg_catalog.jsonb_build_object(
                    'branch_name',v_branch_name,'from_status',v_from,'to_status',v_to
                ),
                'branch-lifecycle/'||v_correlation::text||'/'||m.id::text||'/email',
                'pending',0,5,pg_catalog.clock_timestamp(),v_correlation,
                pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp()
            FROM public.members m
            LEFT JOIN public.member_notification_preferences p
              ON p.tenant_id=m.org_id AND p.member_id=m.id
            WHERE m.org_id=v_tenant AND m.home_branch_id=v_branch
              AND m.is_active IS TRUE AND m.status::text='active'
              AND NULLIF(pg_catalog.btrim(m.email),'') IS NOT NULL
              AND COALESCE(p.email_enabled,true) IS TRUE
              AND p.email_suppressed_at IS NULL
            ON CONFLICT (idempotency_key) DO NOTHING;

            INSERT INTO public.notification_commands(
                command_id,source_outbox_id,tenant_id,branch_id,member_id,effect_type,
                channel,template_key,template_data,idempotency_key,status,attempt_count,
                max_attempts,next_attempt_at,correlation_id,created_at,updated_at
            )
            SELECT
                pg_catalog.gen_random_uuid(),p_outbox_id,v_tenant,v_branch,m.id,
                'branch.member_notification','whatsapp','branch_lifecycle_status_changed',
                pg_catalog.jsonb_build_object(
                    'branch_name',v_branch_name,'from_status',v_from,'to_status',v_to
                ),
                'branch-lifecycle/'||v_correlation::text||'/'||m.id::text||'/whatsapp',
                'pending',0,5,pg_catalog.clock_timestamp(),v_correlation,
                pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp()
            FROM public.members m
            JOIN public.member_notification_preferences p
              ON p.tenant_id=m.org_id AND p.member_id=m.id
            WHERE m.org_id=v_tenant AND m.home_branch_id=v_branch
              AND m.is_active IS TRUE AND m.status::text='active'
              AND NULLIF(pg_catalog.btrim(m.phone),'') IS NOT NULL
              AND p.whatsapp_enabled IS TRUE
              AND p.whatsapp_suppressed_at IS NULL
            ON CONFLICT (idempotency_key) DO NOTHING;

            INSERT INTO public.branch_outbox_events(
                outbox_id,tenant_id,branch_id,event_type,payload,created_at,process_after,
                status,attempt_count,max_attempts,correlation_id,leased_by,leased_until
            )
            SELECT c.command_id,c.tenant_id,c.branch_id,'notification.delivery',
                   pg_catalog.jsonb_build_object('command_id',c.command_id::text),
                   pg_catalog.clock_timestamp(),c.next_attempt_at,'pending',0,c.max_attempts,
                   c.correlation_id,NULL,NULL
            FROM public.notification_commands c
            WHERE c.source_outbox_id=p_outbox_id
            ON CONFLICT (outbox_id) DO NOTHING;

            SELECT count(*)::integer INTO v_count
            FROM public.notification_commands c WHERE c.source_outbox_id=p_outbox_id;

            UPDATE public.branch_outbox_events
            SET status='superseded',leased_by=NULL,leased_until=NULL,
                last_error=CASE WHEN v_count=0
                    THEN 'notification_no_eligible_recipients'
                    ELSE 'notification_expanded_to_durable_commands' END
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id
              AND leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification fanout lost lease fence' USING ERRCODE='40001';
            END IF;

            RETURN v_count;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.claim_notification_delivery(
            p_outbox_id uuid,
            p_worker_id uuid
        ) RETURNS TABLE(
            eligible boolean, command_id uuid, tenant_id uuid, branch_id uuid,
            member_id uuid, channel text, destination text, member_name text,
            template_key text, template_data jsonb, idempotency_key text,
            attempt_number integer, provider_code text
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE
            v_command public.notification_commands%ROWTYPE;
            v_destination text;
            v_member_name text;
            v_allowed boolean := false;
            v_attempt integer;
            v_lease_until timestamptz;
        BEGIN
            SELECT o.leased_until INTO v_lease_until
            FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id AND o.event_type='notification.delivery'
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification claim requires a live owned outbox lease' USING ERRCODE='42501';
            END IF;

            SELECT c.* INTO v_command FROM public.notification_commands c
            WHERE c.command_id=p_outbox_id FOR UPDATE;
            IF NOT FOUND OR v_command.status NOT IN ('pending','retry_pending','processing') THEN
                RAISE EXCEPTION 'notification command is not claimable' USING ERRCODE='40001';
            END IF;

            SELECT
                CASE WHEN v_command.channel='email' THEN m.email ELSE m.phone END,
                m.name,
                CASE
                    WHEN v_command.channel='email' THEN
                        NULLIF(pg_catalog.btrim(m.email),'') IS NOT NULL
                        AND COALESCE(p.email_enabled,true) IS TRUE
                        AND p.email_suppressed_at IS NULL
                    WHEN v_command.channel='whatsapp' THEN
                        NULLIF(pg_catalog.btrim(m.phone),'') IS NOT NULL
                        AND COALESCE(p.whatsapp_enabled,false) IS TRUE
                        AND p.whatsapp_suppressed_at IS NULL
                    ELSE false
                END
            INTO v_destination,v_member_name,v_allowed
            FROM public.members m
            LEFT JOIN public.member_notification_preferences p
              ON p.tenant_id=m.org_id AND p.member_id=m.id
            WHERE m.id=v_command.member_id AND m.org_id=v_command.tenant_id
              AND m.home_branch_id=v_command.branch_id
              AND m.is_active IS TRUE AND m.status::text='active';

            IF NOT FOUND OR NOT COALESCE(v_allowed,false) THEN
                UPDATE public.notification_commands
                SET status='cancelled',delivery_outcome='suppressed',completed_at=pg_catalog.clock_timestamp(),
                    leased_by=NULL,leased_until=NULL,last_error='recipient_or_channel_no_longer_eligible',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                UPDATE public.branch_outbox_events
                SET status='superseded',leased_by=NULL,leased_until=NULL,
                    last_error='notification_recipient_suppressed'
                WHERE outbox_id=p_outbox_id AND leased_by=p_worker_id;
                RETURN QUERY SELECT false,v_command.command_id,v_command.tenant_id,v_command.branch_id,
                    v_command.member_id,v_command.channel,NULL::text,NULL::text,v_command.template_key,
                    v_command.template_data,v_command.idempotency_key,v_command.attempt_count,
                    CASE WHEN v_command.channel='email' THEN 'resend' ELSE 'meta_whatsapp' END;
                RETURN;
            END IF;

            v_attempt := v_command.attempt_count + 1;
            IF v_attempt > v_command.max_attempts THEN
                RAISE EXCEPTION 'notification attempt bound exhausted' USING ERRCODE='22023';
            END IF;

            UPDATE public.notification_commands
            SET status='processing',attempt_count=v_attempt,leased_by=p_worker_id,
                leased_until=v_lease_until,attempted_at=pg_catalog.clock_timestamp(),
                provider_code=CASE WHEN channel='email' THEN 'resend' ELSE 'meta_whatsapp' END,
                destination_sha256=pg_catalog.encode(pg_catalog.digest(v_destination,'sha256'),'hex'),
                updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=v_command.command_id;

            RETURN QUERY SELECT true,v_command.command_id,v_command.tenant_id,v_command.branch_id,
                v_command.member_id,v_command.channel,v_destination,v_member_name,v_command.template_key,
                v_command.template_data,v_command.idempotency_key,v_attempt,
                CASE WHEN v_command.channel='email' THEN 'resend' ELSE 'meta_whatsapp' END;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.acknowledge_notification_provider_acceptance(
            p_outbox_id uuid,p_worker_id uuid,p_provider_reference_id text,
            p_request_sha256 text,p_evidence_sha256 text,p_provider_code text
        ) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command public.notification_commands%ROWTYPE;
        BEGIN
            IF NULLIF(pg_catalog.btrim(p_provider_reference_id),'') IS NULL
               OR p_request_sha256 !~ '^[0-9a-f]{64}$'
               OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR p_provider_code NOT IN ('resend','meta_whatsapp') THEN
                RAISE EXCEPTION 'invalid notification provider acknowledgement' USING ERRCODE='22023';
            END IF;
            SELECT c.* INTO v_command FROM public.notification_commands c
            JOIN public.branch_outbox_events o ON o.outbox_id=c.command_id
            WHERE c.command_id=p_outbox_id AND c.status='processing'
              AND c.leased_by=p_worker_id AND c.leased_until>pg_catalog.clock_timestamp()
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp()
            FOR UPDATE OF c;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification acknowledgement requires live command/outbox fence' USING ERRCODE='42501';
            END IF;

            INSERT INTO public.notification_delivery_attempts(
                attempt_id,command_id,tenant_id,attempt_number,outcome,provider_code,
                provider_reference_id,request_sha256,provider_evidence_sha256,created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_command.command_id,v_command.tenant_id,
                v_command.attempt_count,'provider_accepted_nonterminal',p_provider_code,
                p_provider_reference_id,p_request_sha256,p_evidence_sha256,pg_catalog.clock_timestamp()
            ) ON CONFLICT (command_id,attempt_number) DO NOTHING;

            UPDATE public.notification_commands
            SET status='provider_accepted',provider_code=p_provider_code,
                provider_reference_id=p_provider_reference_id,request_sha256=p_request_sha256,
                acknowledgement_evidence_sha256=p_evidence_sha256,
                acknowledged_at=pg_catalog.clock_timestamp(),leased_by=NULL,leased_until=NULL,
                last_error=NULL,updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=v_command.command_id;

            UPDATE public.branch_outbox_events
            SET status='provider_accepted',leased_by=NULL,leased_until=NULL,last_error=NULL
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification acknowledgement lost outbox fence' USING ERRCODE='40001';
            END IF;
            RETURN true;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.record_notification_delivery_failure(
            p_outbox_id uuid,p_worker_id uuid,p_outcome text,p_request_sha256 text,
            p_error_code text,p_provider_code text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE
            v_command public.notification_commands%ROWTYPE;
            v_terminal boolean;
            v_delay integer;
            v_next_status text;
        BEGIN
            IF p_outcome NOT IN ('permanent_rejection','retryable_failure','ambiguous_outcome')
               OR p_request_sha256 !~ '^[0-9a-f]{64}$'
               OR NULLIF(pg_catalog.btrim(p_error_code),'') IS NULL
               OR p_provider_code NOT IN ('resend','meta_whatsapp') THEN
                RAISE EXCEPTION 'invalid notification failure evidence' USING ERRCODE='22023';
            END IF;

            SELECT c.* INTO v_command FROM public.notification_commands c
            JOIN public.branch_outbox_events o ON o.outbox_id=c.command_id
            WHERE c.command_id=p_outbox_id AND c.status='processing'
              AND c.leased_by=p_worker_id AND c.leased_until>pg_catalog.clock_timestamp()
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp()
            FOR UPDATE OF c;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification failure recording requires live fence' USING ERRCODE='42501';
            END IF;

            INSERT INTO public.notification_delivery_attempts(
                attempt_id,command_id,tenant_id,attempt_number,outcome,provider_code,
                request_sha256,error_code,created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_command.command_id,v_command.tenant_id,
                v_command.attempt_count,p_outcome,p_provider_code,p_request_sha256,
                left(p_error_code,160),pg_catalog.clock_timestamp()
            ) ON CONFLICT (command_id,attempt_number) DO NOTHING;

            v_terminal := p_outcome='permanent_rejection' OR v_command.attempt_count>=v_command.max_attempts;
            v_delay := LEAST(1800,30 * (2 ^ GREATEST(v_command.attempt_count-1,0))::integer);
            v_next_status := CASE WHEN v_terminal THEN 'dead_lettered' ELSE 'retry_pending' END;

            UPDATE public.notification_commands
            SET status=v_next_status,leased_by=NULL,leased_until=NULL,
                next_attempt_at=CASE WHEN v_terminal THEN next_attempt_at
                                     ELSE pg_catalog.clock_timestamp()+v_delay*INTERVAL '1 second' END,
                last_error=left(p_error_code,2000),
                dead_letter_reason=CASE WHEN v_terminal THEN left(p_error_code,500) ELSE NULL END,
                completed_at=CASE WHEN v_terminal THEN pg_catalog.clock_timestamp() ELSE NULL END,
                updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=v_command.command_id;

            UPDATE public.branch_outbox_events
            SET status=CASE WHEN v_terminal THEN 'dead_lettered' ELSE 'pending' END,
                process_after=CASE WHEN v_terminal THEN process_after
                                   ELSE pg_catalog.clock_timestamp()+v_delay*INTERVAL '1 second' END,
                leased_by=NULL,leased_until=NULL,last_error=left(p_error_code,2000)
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification failure recording lost outbox fence' USING ERRCODE='40001';
            END IF;
            RETURN v_next_status;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.apply_notification_provider_event(
            p_provider_code text,p_provider_event_id text,p_provider_reference_id text,
            p_event_type text,p_event_created_at timestamptz,p_evidence_sha256 text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command public.notification_commands%ROWTYPE;
        DECLARE v_result text;
        BEGIN
            IF p_provider_code<>'resend'
               OR NULLIF(pg_catalog.btrim(p_provider_event_id),'') IS NULL
               OR NULLIF(pg_catalog.btrim(p_provider_reference_id),'') IS NULL
               OR p_event_created_at IS NULL
               OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR p_event_type NOT IN (
                    'email.sent','email.delivered','email.delivery_delayed','email.bounced',
                    'email.complained','email.failed','email.suppressed','email.opened','email.clicked'
               ) THEN
                RAISE EXCEPTION 'invalid notification provider event' USING ERRCODE='22023';
            END IF;

            INSERT INTO public.notification_provider_events(
                provider_code,provider_event_id,provider_reference_id,event_type,
                event_created_at,evidence_sha256,received_at
            ) VALUES (
                p_provider_code,p_provider_event_id,p_provider_reference_id,p_event_type,
                p_event_created_at,p_evidence_sha256,pg_catalog.clock_timestamp()
            ) ON CONFLICT (provider_code,provider_event_id) DO NOTHING;
            IF NOT FOUND THEN RETURN 'duplicate'; END IF;

            SELECT c.* INTO v_command FROM public.notification_commands c
            WHERE c.provider_code=p_provider_code AND c.provider_reference_id=p_provider_reference_id
            FOR UPDATE;
            IF NOT FOUND THEN RETURN 'pending_reference'; END IF;

            UPDATE public.notification_provider_events
            SET command_id=v_command.command_id
            WHERE provider_code=p_provider_code AND provider_event_id=p_provider_event_id;

            IF v_command.last_provider_event_at IS NOT NULL
               AND p_event_created_at < v_command.last_provider_event_at THEN
                RETURN 'ignored_stale';
            END IF;

            IF p_event_type IN ('email.delivered','email.opened','email.clicked') THEN
                UPDATE public.notification_commands
                SET status='succeeded',delivery_outcome='delivered',provider_event_id=p_provider_event_id,
                    acknowledgement_evidence_sha256=p_evidence_sha256,last_provider_event_at=p_event_created_at,
                    completed_at=COALESCE(completed_at,pg_catalog.clock_timestamp()),
                    last_error=NULL,dead_letter_reason=NULL,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                UPDATE public.branch_outbox_events
                SET status='delivered',last_error=NULL
                WHERE outbox_id=v_command.command_id AND status='provider_accepted';
                v_result := 'succeeded';
            ELSIF p_event_type IN ('email.bounced','email.complained','email.failed','email.suppressed') THEN
                UPDATE public.notification_commands
                SET status='dead_lettered',delivery_outcome=substring(p_event_type from 7),
                    provider_event_id=p_provider_event_id,
                    acknowledgement_evidence_sha256=p_evidence_sha256,last_provider_event_at=p_event_created_at,
                    completed_at=pg_catalog.clock_timestamp(),last_error=p_event_type,
                    dead_letter_reason=p_event_type,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                UPDATE public.branch_outbox_events
                SET status='dead_lettered',last_error=p_event_type
                WHERE outbox_id=v_command.command_id AND status='provider_accepted';
                IF p_event_type IN ('email.bounced','email.complained','email.suppressed') THEN
                    INSERT INTO public.member_notification_preferences(
                        tenant_id,member_id,email_enabled,whatsapp_enabled,email_suppressed_at,
                        suppression_reason,updated_at
                    ) VALUES (
                        v_command.tenant_id,v_command.member_id,true,false,pg_catalog.clock_timestamp(),
                        p_event_type,pg_catalog.clock_timestamp()
                    ) ON CONFLICT (tenant_id,member_id) DO UPDATE
                      SET email_suppressed_at=EXCLUDED.email_suppressed_at,
                          suppression_reason=EXCLUDED.suppression_reason,
                          updated_at=EXCLUDED.updated_at;
                END IF;
                v_result := 'dead_lettered';
            ELSE
                UPDATE public.notification_commands
                SET provider_event_id=p_provider_event_id,last_provider_event_at=p_event_created_at,
                    acknowledgement_evidence_sha256=p_evidence_sha256,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                v_result := 'provider_accepted';
            END IF;
            RETURN v_result;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.enqueue_notification_reconciliation(p_batch_size integer)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_count integer;
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance',true) IS DISTINCT FROM 'lifecycle' THEN
                RAISE EXCEPTION 'notification reconciliation requires lifecycle maintenance context' USING ERRCODE='42501';
            END IF;
            IF p_batch_size IS NULL OR p_batch_size<1 OR p_batch_size>100 THEN
                RAISE EXCEPTION 'notification reconciliation batch size must be 1..100' USING ERRCODE='22023';
            END IF;
            WITH candidates AS (
                SELECT c.command_id,c.tenant_id,c.branch_id,c.correlation_id
                FROM public.notification_commands c
                WHERE c.status='provider_accepted'
                  AND c.acknowledged_at < pg_catalog.clock_timestamp()-INTERVAL '5 minutes'
                  AND NOT EXISTS (
                    SELECT 1 FROM public.branch_outbox_events o
                    WHERE o.event_type='notification.reconcile'
                      AND o.payload->>'command_id'=c.command_id::text
                      AND o.status IN ('pending','processing')
                  )
                ORDER BY c.acknowledged_at,c.command_id LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            ), inserted AS (
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,created_at,process_after,
                    status,attempt_count,max_attempts,correlation_id,leased_by,leased_until
                )
                SELECT pg_catalog.gen_random_uuid(),tenant_id,branch_id,'notification.reconcile',
                    pg_catalog.jsonb_build_object('command_id',command_id::text),
                    pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp(),'pending',0,5,
                    correlation_id,NULL,NULL FROM candidates RETURNING 1
            ) SELECT count(*)::integer INTO v_count FROM inserted;
            RETURN v_count;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.claim_notification_reconciliation(
            p_outbox_id uuid,p_worker_id uuid
        ) RETURNS TABLE(command_id uuid,tenant_id uuid,branch_id uuid,provider_code text,provider_reference_id text)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command_id uuid;
        BEGIN
            SELECT (o.payload->>'command_id')::uuid INTO v_command_id
            FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id AND o.event_type='notification.reconcile'
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation claim requires live lease' USING ERRCODE='42501';
            END IF;
            RETURN QUERY
            SELECT c.command_id,c.tenant_id,c.branch_id,c.provider_code,c.provider_reference_id
            FROM public.notification_commands c
            WHERE c.command_id=v_command_id AND c.status='provider_accepted'
              AND c.provider_reference_id IS NOT NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation command is not provider accepted' USING ERRCODE='P0002';
            END IF;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.apply_notification_reconciliation_result(
            p_outbox_id uuid,p_worker_id uuid,p_command_id uuid,
            p_last_event text,p_evidence_sha256 text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command public.notification_commands%ROWTYPE;
        DECLARE v_result text;
        BEGIN
            IF p_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR p_last_event NOT IN ('sent','delivered','delivery_delayed','bounced','complained','failed','suppressed','opened','clicked') THEN
                RAISE EXCEPTION 'invalid notification reconciliation evidence' USING ERRCODE='22023';
            END IF;
            PERFORM 1 FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id AND o.event_type='notification.reconcile'
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp()
              AND o.payload->>'command_id'=p_command_id::text;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation requires live fenced event' USING ERRCODE='42501';
            END IF;
            SELECT c.* INTO v_command FROM public.notification_commands c
            WHERE c.command_id=p_command_id AND c.status='provider_accepted' FOR UPDATE;
            IF NOT FOUND THEN
                UPDATE public.branch_outbox_events SET status='superseded',leased_by=NULL,leased_until=NULL,
                    last_error='notification_reconciliation_superseded'
                WHERE outbox_id=p_outbox_id AND leased_by=p_worker_id;
                RETURN 'superseded';
            END IF;

            IF p_last_event IN ('delivered','opened','clicked') THEN
                UPDATE public.notification_commands
                SET status='succeeded',delivery_outcome='delivered',
                    acknowledgement_evidence_sha256=p_evidence_sha256,
                    completed_at=COALESCE(completed_at,pg_catalog.clock_timestamp()),
                    updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=p_command_id;
                UPDATE public.branch_outbox_events SET status='delivered',last_error=NULL
                WHERE outbox_id=p_command_id AND status='provider_accepted';
                v_result:='succeeded';
            ELSIF p_last_event IN ('bounced','complained','failed','suppressed') THEN
                UPDATE public.notification_commands
                SET status='dead_lettered',delivery_outcome=p_last_event,
                    acknowledgement_evidence_sha256=p_evidence_sha256,
                    completed_at=pg_catalog.clock_timestamp(),last_error='provider_'||p_last_event,
                    dead_letter_reason='provider_'||p_last_event,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=p_command_id;
                UPDATE public.branch_outbox_events SET status='dead_lettered',last_error='provider_'||p_last_event
                WHERE outbox_id=p_command_id AND status='provider_accepted';
                v_result:='dead_lettered';
            ELSE
                UPDATE public.notification_commands
                SET acknowledgement_evidence_sha256=p_evidence_sha256,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=p_command_id;
                v_result:='provider_accepted';
            END IF;

            UPDATE public.branch_outbox_events
            SET status='delivered',leased_by=NULL,leased_until=NULL,last_error=NULL
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation lost fence' USING ERRCODE='40001';
            END IF;
            RETURN v_result;
        END;
        $function$;
        """
    )

    for signature in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

    for signature in (
        _FUNCTIONS[0], _FUNCTIONS[1], _FUNCTIONS[2], _FUNCTIONS[3],
        _FUNCTIONS[6], _FUNCTIONS[7],
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTIONS[4]} TO app_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTIONS[5]} TO lifecycle_maintenance_runtime")
    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    for table in (_COMMANDS, _ATTEMPTS, _WEBHOOKS, _PREFS):
        for role_name in (_APP, "auth_runtime", _WORKER, _MAINTENANCE):
            has_any = bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'SELECT')
                        OR pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'INSERT')
                        OR pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'UPDATE')
                        OR pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'DELETE')
                    """
                ),
                {"role": role_name, "relation": table},
            ).scalar_one()
            if has_any:
                raise RuntimeError(f"w07 leaked direct notification table ACL: {role_name} {table}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)
    _install_outbox_status_contract(_P4C_OUTBOX_STATUSES)
    _create_tables()
    _create_functions()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)

    unsafe = 0
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass(:r) IS NOT NULL"), {"r": _COMMANDS}).scalar_one():
        unsafe = int(bind.execute(
            sa.text(
                """
                SELECT count(*) FROM public.notification_commands
                WHERE status IN ('processing','provider_accepted','succeeded','dead_lettered')
                   OR provider_reference_id IS NOT NULL
                   OR acknowledgement_evidence_sha256 IS NOT NULL
                """
            )
        ).scalar_one())
    if unsafe:
        raise RuntimeError("w07 downgrade refuses loss of live/provider-backed notification state")

    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("RESET ROLE")

    op.execute("REVOKE SELECT (id,org_id,home_branch_id,name,email,phone,status,is_active) ON public.members FROM app_security_owner")
    op.execute("REVOKE SELECT (history_id,branch_id,from_status,to_status,correlation_id,changed_at) ON public.branch_status_history FROM app_security_owner")
    op.execute("DROP TABLE public.notification_provider_events")
    op.execute("DROP TABLE public.notification_delivery_attempts")
    op.execute("DROP TABLE public.notification_commands")
    op.execute("DROP TABLE public.member_notification_preferences")
    _install_outbox_status_contract(_PREDECESSOR_OUTBOX_STATUSES)
