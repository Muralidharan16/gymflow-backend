"""Harden P4C notification reconciliation, provider-event precedence and DLQ recovery.

Revision ID: x07d8e9f0a38
Revises: w07d8e9f0a37
Create Date: 2026-08-17

Provider acceptance remains non-terminal. Global discovery belongs to the
maintenance identity, provider access belongs to the worker, and operator replay
is permitted only when durable attempt evidence proves there is no ambiguous or
accepted external effect to duplicate.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "x07d8e9f0a38"
down_revision = "w07d8e9f0a37"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_APP = "app_runtime"
_REPLAY_MEMBER_POLICY = "p4c_notification_replay_security_owner_select"
_REPLAY_COMMAND_GUC = "app.notification_replay_command_id"

_OLD_WEBHOOK = "app_secure.apply_resend_notification_event(text,text,text,timestamptz,text)"
_V2_WEBHOOK = "app_secure.apply_resend_notification_event_v2(text,text,text,timestamptz,text)"
_ENQUEUE = "app_secure.enqueue_notification_reconciliation(integer)"
_CLAIM = "app_secure.claim_notification_reconciliation(uuid,uuid)"
_COMPLETE = "app_secure.complete_notification_reconciliation(uuid,uuid,text,text)"
_FAIL = "app_secure.record_notification_reconciliation_failure(uuid,uuid,text,boolean)"
_REQUEUE = "app_secure.requeue_dead_lettered_notification(uuid,text)"
_LIST_DLQ = "app_secure.list_notification_dead_letters(integer)"


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
        raise RuntimeError("x07 P4C migration requires migration_owner")
    if any(bool(row[key]) for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
    )):
        raise RuntimeError("x07 migration_owner violates reduced-role contract")
    for role_name in (_SECURITY_OWNER, _WORKER, _MAINTENANCE, _APP, "auth_runtime"):
        role = bind.execute(
            sa.text(
                """
                SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
                FROM pg_catalog.pg_roles WHERE rolname=:role
                """
            ),
            {"role": role_name},
        ).mappings().one_or_none()
        if role is None or any(bool(role[key]) for key in role):
            raise RuntimeError(f"x07 reduced-role contract drift: {role_name}")


def _require_predecessor(bind) -> None:
    for relation in (
        "public.notification_commands",
        "public.notification_delivery_attempts",
        "public.notification_provider_events",
        "public.member_notification_preferences",
        "public.branch_outbox_events",
        "public.members",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"x07 missing predecessor relation {relation}")
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('public.notification_operator_actions') IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("x07 notification operator action relation already exists")
    if bind.execute(
        sa.text(
            """
            SELECT EXISTS(
                SELECT 1 FROM pg_catalog.pg_policy
                WHERE polrelid='public.members'::regclass AND polname=:policy
            )
            """
        ),
        {"policy": _REPLAY_MEMBER_POLICY},
    ).scalar_one():
        raise RuntimeError("x07 notification replay member policy already exists")


def _create_storage() -> None:
    op.execute(
        """
        ALTER TABLE public.notification_commands
          ADD COLUMN reconciliation_pending boolean NOT NULL DEFAULT false,
          ADD COLUMN reconciliation_attempt_count integer NOT NULL DEFAULT 0,
          ADD COLUMN reconciliation_next_at timestamptz,
          ADD COLUMN last_reconciled_at timestamptz,
          ADD COLUMN operator_replay_count integer NOT NULL DEFAULT 0,
          ADD CONSTRAINT notification_reconciliation_attempt_bounds
            CHECK (reconciliation_attempt_count BETWEEN 0 AND 1000),
          ADD CONSTRAINT notification_operator_replay_bounds
            CHECK (operator_replay_count BETWEEN 0 AND 100)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_notification_commands_reconcile_due
          ON public.notification_commands(status,reconciliation_pending,reconciliation_next_at,acknowledged_at)
          WHERE status='provider_accepted'
        """
    )
    op.execute(
        """
        CREATE TABLE public.notification_operator_actions (
            action_id uuid PRIMARY KEY,
            command_id uuid NOT NULL REFERENCES public.notification_commands(command_id) ON DELETE RESTRICT,
            tenant_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            action text NOT NULL CHECK (action IN ('requeue_dead_letter')),
            reason text NOT NULL CHECK (char_length(reason) BETWEEN 3 AND 500),
            actor_principal_id text,
            database_role text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
        )
        """
    )
    op.execute("ALTER TABLE public.notification_operator_actions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.notification_operator_actions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY notification_operator_actions_security_owner_policy
        ON public.notification_operator_actions FOR ALL TO app_security_owner
        USING (true) WITH CHECK (true)
        """
    )
    op.execute("REVOKE ALL ON TABLE public.notification_operator_actions FROM PUBLIC")
    op.execute(
        """
        REVOKE ALL ON TABLE public.notification_operator_actions
        FROM app_runtime,auth_runtime,worker_runtime,lifecycle_maintenance_runtime
        """
    )
    op.execute(
        "GRANT SELECT,INSERT ON TABLE public.notification_operator_actions TO app_security_owner"
    )
    op.execute(
        """
        CREATE POLICY p4c_notification_replay_security_owner_select
        ON public.members
        FOR SELECT TO app_security_owner
        USING (
            pg_catalog.pg_has_role(
                session_user,
                'lifecycle_maintenance_runtime',
                'MEMBER'
            )
            AND pg_catalog.pg_input_is_valid(
                NULLIF(
                    pg_catalog.current_setting('app.notification_replay_command_id',true),
                    ''
                ),
                'uuid'
            )
            AND EXISTS (
                SELECT 1
                FROM public.notification_commands AS command_data
                WHERE command_data.command_id=CAST(
                    NULLIF(
                        pg_catalog.current_setting('app.notification_replay_command_id',true),
                        ''
                    ) AS uuid
                )
                  AND command_data.status='dead_lettered'
                  AND command_data.member_id=members.id
                  AND command_data.tenant_id=members.org_id
                  AND command_data.branch_id=members.home_branch_id
            )
        )
        """
    )


def _create_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_OLD_WEBHOOK} FROM app_runtime")

    op.execute(
        r"""
        CREATE FUNCTION app_secure.apply_resend_notification_event_v2(
            p_event_id text,p_provider_reference_id text,p_event_type text,
            p_event_created_at timestamptz,p_evidence_sha256 text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE
            v_command public.notification_commands%ROWTYPE;
            v_stale boolean := false;
            v_result text;
        BEGIN
            IF NULLIF(pg_catalog.btrim(p_event_id),'') IS NULL
               OR NULLIF(pg_catalog.btrim(p_provider_reference_id),'') IS NULL
               OR p_event_created_at IS NULL
               OR p_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR p_event_type NOT IN (
                    'email.sent','email.delivered','email.delivery_delayed','email.bounced',
                    'email.complained','email.failed','email.suppressed','email.opened','email.clicked'
               ) THEN
                RAISE EXCEPTION 'invalid Resend notification event' USING ERRCODE='22023';
            END IF;

            INSERT INTO public.notification_provider_events(
                provider_code,provider_event_id,provider_reference_id,event_type,
                event_created_at,evidence_sha256,received_at
            ) VALUES (
                'resend',p_event_id,p_provider_reference_id,p_event_type,
                p_event_created_at,p_evidence_sha256,pg_catalog.clock_timestamp()
            ) ON CONFLICT(provider_code,provider_event_id) DO NOTHING;
            IF NOT FOUND THEN RETURN 'duplicate'; END IF;

            SELECT c.* INTO v_command FROM public.notification_commands c
            WHERE c.provider_code='resend' AND c.provider_reference_id=p_provider_reference_id
            FOR UPDATE;
            IF NOT FOUND THEN RETURN 'pending_reference'; END IF;

            UPDATE public.notification_provider_events SET command_id=v_command.command_id
            WHERE provider_code='resend' AND provider_event_id=p_event_id;

            IF p_event_type IN ('email.bounced','email.complained','email.suppressed') THEN
                INSERT INTO public.member_notification_preferences(
                    tenant_id,member_id,email_enabled,whatsapp_enabled,email_suppressed_at,
                    suppression_reason,updated_at
                ) VALUES (
                    v_command.tenant_id,v_command.member_id,true,false,pg_catalog.clock_timestamp(),
                    p_event_type,pg_catalog.clock_timestamp()
                ) ON CONFLICT(tenant_id,member_id) DO UPDATE
                  SET email_suppressed_at=EXCLUDED.email_suppressed_at,
                      suppression_reason=EXCLUDED.suppression_reason,
                      updated_at=EXCLUDED.updated_at;
            END IF;

            v_stale := v_command.last_provider_event_at IS NOT NULL
                       AND p_event_created_at < v_command.last_provider_event_at;
            IF v_stale THEN
                RETURN 'ignored_stale';
            END IF;

            IF v_command.status='succeeded' AND v_command.delivery_outcome='delivered' THEN
                UPDATE public.notification_commands
                SET provider_event_id=p_event_id,provider_evidence_sha256=p_evidence_sha256,
                    last_provider_event_at=p_event_created_at,updated_at=pg_catalog.clock_timestamp(),
                    reconciliation_pending=false,reconciliation_next_at=NULL
                WHERE command_id=v_command.command_id;
                IF p_event_type IN ('email.bounced','email.complained','email.failed','email.suppressed') THEN
                    RETURN 'recorded_after_delivery';
                END IF;
                RETURN 'succeeded';
            END IF;

            IF p_event_type IN ('email.delivered','email.opened','email.clicked') THEN
                UPDATE public.notification_commands
                SET status='succeeded',delivery_outcome='delivered',provider_event_id=p_event_id,
                    provider_evidence_sha256=p_evidence_sha256,last_provider_event_at=p_event_created_at,
                    completed_at=COALESCE(completed_at,pg_catalog.clock_timestamp()),
                    last_error=NULL,dead_letter_reason=NULL,updated_at=pg_catalog.clock_timestamp(),
                    reconciliation_pending=false,reconciliation_next_at=NULL
                WHERE command_id=v_command.command_id;
                UPDATE public.branch_outbox_events SET status='delivered',last_error=NULL,
                    leased_by=NULL,leased_until=NULL
                WHERE outbox_id=v_command.command_id
                  AND status IN ('provider_accepted','dead_lettered');
                v_result := 'succeeded';
            ELSIF p_event_type IN ('email.bounced','email.complained','email.failed','email.suppressed') THEN
                UPDATE public.notification_commands
                SET status='dead_lettered',delivery_outcome=substring(p_event_type from 7),
                    provider_event_id=p_event_id,provider_evidence_sha256=p_evidence_sha256,
                    last_provider_event_at=p_event_created_at,completed_at=pg_catalog.clock_timestamp(),
                    last_error=p_event_type,dead_letter_reason=p_event_type,
                    updated_at=pg_catalog.clock_timestamp(),reconciliation_pending=false,
                    reconciliation_next_at=NULL
                WHERE command_id=v_command.command_id;
                UPDATE public.branch_outbox_events SET status='dead_lettered',last_error=p_event_type,
                    leased_by=NULL,leased_until=NULL
                WHERE outbox_id=v_command.command_id AND status='provider_accepted';
                v_result := 'dead_lettered';
            ELSE
                UPDATE public.notification_commands
                SET provider_event_id=p_event_id,provider_evidence_sha256=p_evidence_sha256,
                    last_provider_event_at=p_event_created_at,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                v_result := 'provider_accepted';
            END IF;
            RETURN v_result;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.enqueue_notification_reconciliation(p_batch_size integer)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_count integer;
        BEGIN
            IF p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 500 THEN
                RAISE EXCEPTION 'notification reconciliation batch_size must be in [1,500]'
                  USING ERRCODE='22023';
            END IF;
            WITH candidates AS (
                SELECT c.command_id
                FROM public.notification_commands c
                WHERE c.status='provider_accepted'
                  AND c.provider_code='resend'
                  AND c.provider_reference_id IS NOT NULL
                  AND c.reconciliation_pending IS FALSE
                  AND c.acknowledged_at <= pg_catalog.clock_timestamp()-INTERVAL '2 minutes'
                  AND COALESCE(c.reconciliation_next_at,c.acknowledged_at) <= pg_catalog.clock_timestamp()
                ORDER BY COALESCE(c.reconciliation_next_at,c.acknowledged_at),c.command_id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            ), marked AS (
                UPDATE public.notification_commands c
                SET reconciliation_pending=true,updated_at=pg_catalog.clock_timestamp()
                FROM candidates q WHERE c.command_id=q.command_id
                RETURNING c.command_id,c.tenant_id,c.branch_id,c.correlation_id
            ), inserted AS (
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,created_at,process_after,
                    status,attempt_count,max_attempts,correlation_id,leased_by,leased_until
                )
                SELECT pg_catalog.gen_random_uuid(),m.tenant_id,m.branch_id,'notification.reconcile',
                       pg_catalog.jsonb_build_object('command_id',m.command_id::text),
                       pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp(),
                       'pending',0,8,m.correlation_id,NULL,NULL
                FROM marked m
                RETURNING 1
            )
            SELECT count(*)::integer INTO v_count FROM inserted;
            RETURN v_count;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.claim_notification_reconciliation(p_outbox_id uuid,p_worker_id uuid)
        RETURNS TABLE(command_id uuid,tenant_id uuid,provider_reference_id text)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command_id uuid; v_tenant uuid;
        BEGIN
            SELECT NULLIF(o.payload->>'command_id','')::uuid,o.tenant_id
              INTO v_command_id,v_tenant
            FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id AND o.event_type='notification.reconcile'
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND OR v_command_id IS NULL THEN
                RAISE EXCEPTION 'notification reconciliation requires live owned lease'
                  USING ERRCODE='42501';
            END IF;
            RETURN QUERY
            UPDATE public.notification_commands c
            SET reconciliation_attempt_count=c.reconciliation_attempt_count+1,
                updated_at=pg_catalog.clock_timestamp()
            WHERE c.command_id=v_command_id AND c.tenant_id=v_tenant
              AND c.status='provider_accepted' AND c.reconciliation_pending IS TRUE
              AND c.provider_code='resend' AND c.provider_reference_id IS NOT NULL
            RETURNING c.command_id,c.tenant_id,c.provider_reference_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation command is not claimable'
                  USING ERRCODE='40001';
            END IF;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.complete_notification_reconciliation(
            p_outbox_id uuid,p_worker_id uuid,p_last_event text,p_evidence_sha256 text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command public.notification_commands%ROWTYPE; v_result text; v_event_id text;
        BEGIN
            IF p_last_event NOT IN (
                'delivered','bounced','complained','failed','suppressed','opened','clicked',
                'sent','delivery_delayed','queued','scheduled'
            ) OR p_evidence_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'invalid notification reconciliation evidence' USING ERRCODE='22023';
            END IF;
            SELECT c.* INTO v_command
            FROM public.notification_commands c
            JOIN public.branch_outbox_events o
              ON o.outbox_id=p_outbox_id
             AND o.event_type='notification.reconcile'
             AND NULLIF(o.payload->>'command_id','')::uuid=c.command_id
            WHERE o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp()
              AND c.tenant_id=o.tenant_id AND c.status='provider_accepted'
              AND c.reconciliation_pending IS TRUE
            FOR UPDATE OF c;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation completion lost fence' USING ERRCODE='42501';
            END IF;

            v_event_id := 'reconcile/'||p_outbox_id::text;
            IF p_last_event IN ('delivered','bounced','complained','failed','suppressed','opened','clicked') THEN
                SELECT app_secure.apply_resend_notification_event_v2(
                    v_event_id,v_command.provider_reference_id,'email.'||p_last_event,
                    pg_catalog.clock_timestamp(),p_evidence_sha256
                ) INTO v_result;
                UPDATE public.notification_commands
                SET reconciliation_pending=false,last_reconciled_at=pg_catalog.clock_timestamp(),
                    reconciliation_next_at=NULL,updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
            ELSE
                INSERT INTO public.notification_provider_events(
                    provider_code,provider_event_id,provider_reference_id,command_id,event_type,
                    event_created_at,evidence_sha256,received_at
                ) VALUES (
                    'resend',v_event_id,v_command.provider_reference_id,v_command.command_id,
                    'reconciliation.'||p_last_event,pg_catalog.clock_timestamp(),p_evidence_sha256,
                    pg_catalog.clock_timestamp()
                ) ON CONFLICT(provider_code,provider_event_id) DO NOTHING;
                UPDATE public.notification_commands
                SET provider_evidence_sha256=p_evidence_sha256,
                    reconciliation_pending=false,last_reconciled_at=pg_catalog.clock_timestamp(),
                    reconciliation_next_at=pg_catalog.clock_timestamp()+INTERVAL '5 minutes',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE command_id=v_command.command_id;
                v_result := 'provider_accepted';
            END IF;

            UPDATE public.branch_outbox_events
            SET status='delivered',leased_by=NULL,leased_until=NULL,last_error=NULL
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id
              AND leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation completion lost outbox fence'
                  USING ERRCODE='40001';
            END IF;
            RETURN v_result;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.record_notification_reconciliation_failure(
            p_outbox_id uuid,p_worker_id uuid,p_error_code text,p_permanent boolean
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command_id uuid; v_attempt integer; v_max integer; v_exhausted boolean; v_delay integer;
        BEGIN
            IF NULLIF(pg_catalog.btrim(p_error_code),'') IS NULL THEN
                RAISE EXCEPTION 'notification reconciliation error_code is required' USING ERRCODE='22023';
            END IF;
            SELECT NULLIF(o.payload->>'command_id','')::uuid,o.attempt_count,o.max_attempts
              INTO v_command_id,v_attempt,v_max
            FROM public.branch_outbox_events o
            WHERE o.outbox_id=p_outbox_id AND o.event_type='notification.reconcile'
              AND o.status='processing' AND o.leased_by=p_worker_id
              AND o.leased_until>pg_catalog.clock_timestamp()
            FOR UPDATE;
            IF NOT FOUND OR v_command_id IS NULL THEN
                RAISE EXCEPTION 'notification reconciliation failure lost fence' USING ERRCODE='42501';
            END IF;
            v_exhausted := COALESCE(p_permanent,false) OR v_attempt>=v_max;
            v_delay := CASE WHEN v_exhausted THEN 3600
                            ELSE LEAST(1800,30*CAST(power(2,GREATEST(v_attempt-1,0)) AS integer)) END;
            UPDATE public.notification_commands
            SET reconciliation_pending=NOT v_exhausted,
                last_reconciled_at=pg_catalog.clock_timestamp(),
                reconciliation_next_at=pg_catalog.clock_timestamp()+v_delay*INTERVAL '1 second',
                last_error=left(p_error_code,2000),updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=v_command_id AND status='provider_accepted';
            UPDATE public.branch_outbox_events
            SET status=CASE WHEN v_exhausted THEN 'dead_lettered' ELSE 'pending' END,
                process_after=pg_catalog.clock_timestamp()+v_delay*INTERVAL '1 second',
                leased_by=NULL,leased_until=NULL,last_error=left(p_error_code,2000)
            WHERE outbox_id=p_outbox_id AND status='processing' AND leased_by=p_worker_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification reconciliation failure lost outbox update fence'
                  USING ERRCODE='40001';
            END IF;
            RETURN CASE WHEN v_exhausted THEN 'dead_lettered' ELSE 'retry' END;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.requeue_dead_lettered_notification(p_command_id uuid,p_reason text)
        RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE v_command public.notification_commands%ROWTYPE; v_allowed boolean; v_new_max integer;
        BEGIN
            IF NULLIF(pg_catalog.btrim(p_reason),'') IS NULL OR char_length(pg_catalog.btrim(p_reason))>500 THEN
                RAISE EXCEPTION 'operator replay reason must be between 1 and 500 characters'
                  USING ERRCODE='22023';
            END IF;
            SELECT c.* INTO v_command FROM public.notification_commands c
            WHERE c.command_id=p_command_id AND c.status='dead_lettered' FOR UPDATE;
            IF NOT FOUND THEN RETURN false; END IF;
            IF v_command.provider_reference_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM public.notification_delivery_attempts a
                WHERE a.command_id=p_command_id
                  AND a.outcome IN ('ambiguous_outcome','provider_accepted_nonterminal','definite_success')
            ) THEN
                RAISE EXCEPTION 'dead-letter replay refused: external effect may already exist'
                  USING ERRCODE='55000';
            END IF;
            PERFORM pg_catalog.set_config(
                'app.notification_replay_command_id',p_command_id::text,true
            );
            SELECT NULLIF(pg_catalog.btrim(m.email),'') IS NOT NULL
                   AND m.is_active IS TRUE AND m.status::text='active'
                   AND m.home_branch_id=v_command.branch_id
                   AND COALESCE(p.email_enabled,true) IS TRUE AND p.email_suppressed_at IS NULL
              INTO v_allowed
            FROM public.members m
            LEFT JOIN public.member_notification_preferences p
              ON p.tenant_id=m.org_id AND p.member_id=m.id
            WHERE m.id=v_command.member_id AND m.org_id=v_command.tenant_id;
            PERFORM pg_catalog.set_config('app.notification_replay_command_id','',true);
            IF NOT COALESCE(v_allowed,false) THEN
                RAISE EXCEPTION 'dead-letter replay refused: current recipient is not eligible'
                  USING ERRCODE='55000';
            END IF;
            v_new_max := LEAST(20,GREATEST(v_command.max_attempts,v_command.attempt_count+3));
            IF v_new_max<=v_command.attempt_count THEN
                RAISE EXCEPTION 'dead-letter replay attempt budget is exhausted' USING ERRCODE='55000';
            END IF;
            UPDATE public.notification_commands
            SET status='retry_pending',max_attempts=v_new_max,next_attempt_at=pg_catalog.clock_timestamp(),
                dead_letter_reason=NULL,completed_at=NULL,last_error='operator_requeued_from_authoritative_state',
                operator_replay_count=operator_replay_count+1,updated_at=pg_catalog.clock_timestamp()
            WHERE command_id=p_command_id;
            UPDATE public.branch_outbox_events
            SET status='pending',max_attempts=v_new_max,process_after=pg_catalog.clock_timestamp(),
                leased_by=NULL,leased_until=NULL,last_error='operator_requeued_from_authoritative_state'
            WHERE outbox_id=p_command_id AND status='dead_lettered';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'dead-letter replay requires matching dead-lettered outbox' USING ERRCODE='55000';
            END IF;
            INSERT INTO public.notification_operator_actions(
                action_id,command_id,tenant_id,action,reason,actor_principal_id,database_role,created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(),p_command_id,v_command.tenant_id,'requeue_dead_letter',
                pg_catalog.btrim(p_reason),NULLIF(pg_catalog.current_setting('app.current_user',true),''),
                session_user::text,pg_catalog.clock_timestamp()
            );
            RETURN true;
        END;
        $function$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.list_notification_dead_letters(p_limit integer)
        RETURNS TABLE(
            command_id uuid,tenant_id uuid,branch_id uuid,member_id uuid,channel text,
            attempt_count integer,max_attempts integer,dead_letter_reason text,created_at timestamptz
        )
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        BEGIN
            IF p_limit IS NULL OR p_limit<1 OR p_limit>500 THEN
                RAISE EXCEPTION 'dead-letter list limit must be in [1,500]' USING ERRCODE='22023';
            END IF;
            RETURN QUERY SELECT c.command_id,c.tenant_id,c.branch_id,c.member_id,c.channel,
                c.attempt_count,c.max_attempts,c.dead_letter_reason,c.created_at
            FROM public.notification_commands c WHERE c.status='dead_lettered'
            ORDER BY c.completed_at NULLS LAST,c.created_at,c.command_id LIMIT p_limit;
        END;
        $function$;
        """
    )

    for signature in (_V2_WEBHOOK,_ENQUEUE,_CLAIM,_COMPLETE,_FAIL,_REQUEUE,_LIST_DLQ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_V2_WEBHOOK} TO app_runtime")
    for signature in (_CLAIM,_COMPLETE,_FAIL):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime")
    for signature in (_ENQUEUE,_REQUEUE,_LIST_DLQ):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO lifecycle_maintenance_runtime")
    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    for role_name in (_APP,"auth_runtime",_WORKER,_MAINTENANCE):
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_table_privilege(:role,'public.notification_operator_actions'::regclass,'SELECT')
                    OR pg_catalog.has_table_privilege(:role,'public.notification_operator_actions'::regclass,'INSERT')
                    OR pg_catalog.has_table_privilege(:role,'public.notification_operator_actions'::regclass,'UPDATE')
                    OR pg_catalog.has_table_privilege(:role,'public.notification_operator_actions'::regclass,'DELETE')
                """
            ),{"role":role_name},
        ).scalar_one():
            raise RuntimeError(f"x07 leaked direct operator-action ACL: {role_name}")

    replay_policy = bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   ARRAY(
                       SELECT r.rolname::text
                       FROM pg_catalog.pg_roles r
                       WHERE r.oid=ANY(p.polroles)
                       ORDER BY r.rolname
                   ) AS roles
            FROM pg_catalog.pg_policy p
            WHERE p.polrelid='public.members'::regclass AND p.polname=:policy
            """
        ),
        {"policy": _REPLAY_MEMBER_POLICY},
    ).mappings().one_or_none()
    if (
        replay_policy is None
        or replay_policy["command"] != "r"
        or list(replay_policy["roles"]) != [_SECURITY_OWNER]
    ):
        raise RuntimeError("x07 notification replay member policy role/command drift")

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                       :role,'public.members'::regclass,'SELECT'
                   )
                OR pg_catalog.has_any_column_privilege(
                       :role,'public.members'::regclass,'SELECT'
                   )
            """
        ),
        {"role": _MAINTENANCE},
    ).scalar_one():
        raise RuntimeError("x07 leaked direct member SELECT to lifecycle maintenance")


def upgrade() -> None:
    bind=op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)
    _create_storage()
    _create_functions()
    _post_install_proof(bind)


def downgrade() -> None:
    bind=op.get_bind()
    _require_identity_contract(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        has_operator_evidence = bind.execute(
            sa.text("SELECT count(*) FROM public.notification_operator_actions")
        ).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if has_operator_evidence:
        raise RuntimeError("x07 downgrade refuses loss of notification operator audit evidence")
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        has_reconciliation_evidence = bind.execute(
            sa.text(
                """
                SELECT count(*) FROM public.notification_commands
                WHERE reconciliation_pending IS TRUE OR reconciliation_attempt_count>0 OR operator_replay_count>0
                """
            )
        ).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if has_reconciliation_evidence:
        raise RuntimeError("x07 downgrade refuses loss of live notification reconciliation/replay state")

    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in reversed((_V2_WEBHOOK,_ENQUEUE,_CLAIM,_COMPLETE,_FAIL,_REQUEUE,_LIST_DLQ)):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_WEBHOOK} TO app_runtime")
    op.execute("RESET ROLE")
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_replay_security_owner_select ON public.members"
    )
    op.execute("DROP TABLE public.notification_operator_actions")
    op.execute("DROP INDEX public.ix_notification_commands_reconcile_due")
    op.execute(
        """
        ALTER TABLE public.notification_commands
          DROP CONSTRAINT notification_operator_replay_bounds,
          DROP CONSTRAINT notification_reconciliation_attempt_bounds,
          DROP COLUMN operator_replay_count,
          DROP COLUMN last_reconciled_at,
          DROP COLUMN reconciliation_next_at,
          DROP COLUMN reconciliation_attempt_count,
          DROP COLUMN reconciliation_pending
        """
    )
