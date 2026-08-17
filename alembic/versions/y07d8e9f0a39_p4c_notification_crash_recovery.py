"""Make P4C delivery claims reclaim expired in-flight attempts safely.

Revision ID: y07d8e9f0a39
Revises: x07d8e9f0a38
Create Date: 2026-08-17

A worker may die after claiming a command and before persisting provider outcome.
The replacement worker must not strand that command forever. Reclaim records the
expired attempt as ambiguous evidence, preserves the same logical idempotency
key, and starts the next fenced attempt only after the old lease expires.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "y07d8e9f0a39"
down_revision = "x07d8e9f0a38"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_OLD = "app_secure.claim_notification_delivery(uuid,uuid)"
_NEW = "app_secure.claim_notification_delivery_v2(uuid,uuid)"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(row) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("y07 P4C migration requires migration_owner")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_OLD} FROM worker_runtime")
    op.execute(
        r"""
        CREATE FUNCTION app_secure.claim_notification_delivery_v2(
            p_outbox_id uuid,p_worker_id uuid
        ) RETURNS TABLE(
            eligible boolean,command_id uuid,tenant_id uuid,branch_id uuid,member_id uuid,
            channel text,destination text,member_name text,template_key text,template_data jsonb,
            idempotency_key text,attempt_number integer,provider_code text
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
        DECLARE
            v_command public.notification_commands%ROWTYPE;
            v_destination text;
            v_name text;
            v_allowed boolean := false;
            v_attempt integer;
            v_outbox_lease timestamptz;
            v_reclaim boolean := false;
        BEGIN
            SELECT outbox_data.leased_until INTO v_outbox_lease
            FROM public.branch_outbox_events AS outbox_data
            WHERE outbox_data.outbox_id=p_outbox_id
              AND outbox_data.event_type='notification.delivery'
              AND outbox_data.status='processing'
              AND outbox_data.leased_by=p_worker_id
              AND outbox_data.leased_until>pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification claim requires live owned outbox lease' USING ERRCODE='42501';
            END IF;

            SELECT command_data.* INTO v_command
            FROM public.notification_commands AS command_data
            WHERE command_data.command_id=p_outbox_id
              AND (
                    command_data.status IN ('pending','retry_pending')
                    OR (
                        command_data.status='processing'
                        AND command_data.leased_until<=pg_catalog.clock_timestamp()
                    )
                  )
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'notification command is not claimable' USING ERRCODE='40001';
            END IF;

            v_reclaim := v_command.status='processing';
            IF v_reclaim THEN
                INSERT INTO public.notification_delivery_attempts(
                    attempt_id,command_id,tenant_id,attempt_number,outcome,provider_code,
                    request_sha256,error_code,created_at
                ) VALUES (
                    pg_catalog.gen_random_uuid(),v_command.command_id,v_command.tenant_id,
                    v_command.attempt_count,'ambiguous_outcome','resend',repeat('0',64),
                    'worker_lease_expired_commit_unknown',pg_catalog.clock_timestamp()
                ) ON CONFLICT ON CONSTRAINT notification_delivery_attempts_command_id_attempt_number_key
                  DO NOTHING;
            END IF;

            SELECT member_data.email,member_data.name,
                   NULLIF(pg_catalog.btrim(member_data.email),'') IS NOT NULL
                   AND COALESCE(preference_data.email_enabled,true) IS TRUE
                   AND preference_data.email_suppressed_at IS NULL
              INTO v_destination,v_name,v_allowed
            FROM public.members AS member_data
            LEFT JOIN public.member_notification_preferences AS preference_data
              ON preference_data.tenant_id=member_data.org_id
             AND preference_data.member_id=member_data.id
            WHERE member_data.id=v_command.member_id
              AND member_data.org_id=v_command.tenant_id
              AND member_data.home_branch_id=v_command.branch_id
              AND member_data.is_active IS TRUE
              AND member_data.status::text='active';

            IF NOT FOUND OR NOT COALESCE(v_allowed,false) OR v_command.channel<>'email' THEN
                UPDATE public.notification_commands AS command_data
                SET status='cancelled',delivery_outcome='suppressed',
                    completed_at=pg_catalog.clock_timestamp(),last_error='recipient_or_channel_not_eligible',
                    leased_by=NULL,leased_until=NULL,updated_at=pg_catalog.clock_timestamp()
                WHERE command_data.command_id=v_command.command_id;
                UPDATE public.branch_outbox_events AS outbox_data
                SET status='superseded',leased_by=NULL,leased_until=NULL,
                    last_error='notification_recipient_suppressed'
                WHERE outbox_data.outbox_id=p_outbox_id
                  AND outbox_data.status='processing'
                  AND outbox_data.leased_by=p_worker_id;
                RETURN QUERY SELECT false,v_command.command_id,v_command.tenant_id,v_command.branch_id,
                    v_command.member_id,v_command.channel,NULL::text,NULL::text,v_command.template_key,
                    v_command.template_data,v_command.idempotency_key,v_command.attempt_count,'resend'::text;
                RETURN;
            END IF;

            IF v_command.attempt_count>=v_command.max_attempts THEN
                UPDATE public.notification_commands AS command_data
                SET status='dead_lettered',leased_by=NULL,leased_until=NULL,
                    completed_at=pg_catalog.clock_timestamp(),
                    dead_letter_reason='attempt_budget_exhausted_after_crash_recovery',
                    last_error='attempt_budget_exhausted_after_crash_recovery',
                    updated_at=pg_catalog.clock_timestamp()
                WHERE command_data.command_id=v_command.command_id;
                UPDATE public.branch_outbox_events AS outbox_data
                SET status='dead_lettered',leased_by=NULL,leased_until=NULL,
                    last_error='attempt_budget_exhausted_after_crash_recovery'
                WHERE outbox_data.outbox_id=p_outbox_id
                  AND outbox_data.status='processing'
                  AND outbox_data.leased_by=p_worker_id;
                RETURN QUERY SELECT false,v_command.command_id,v_command.tenant_id,v_command.branch_id,
                    v_command.member_id,v_command.channel,NULL::text,NULL::text,v_command.template_key,
                    v_command.template_data,v_command.idempotency_key,v_command.attempt_count,'resend'::text;
                RETURN;
            END IF;

            v_attempt := v_command.attempt_count+1;
            UPDATE public.notification_commands AS command_data
            SET status='processing',attempt_count=v_attempt,leased_by=p_worker_id,
                leased_until=v_outbox_lease,attempted_at=pg_catalog.clock_timestamp(),
                provider_code='resend',updated_at=pg_catalog.clock_timestamp()
            WHERE command_data.command_id=v_command.command_id;

            RETURN QUERY SELECT true,v_command.command_id,v_command.tenant_id,v_command.branch_id,
                v_command.member_id,v_command.channel,v_destination,v_name,v_command.template_key,
                v_command.template_data,v_command.idempotency_key,v_attempt,'resend'::text;
        END;
        $function$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_NEW} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW} TO worker_runtime")
    op.execute("RESET ROLE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    unsafe = bind.execute(
        sa.text(
            """
            SELECT count(*) FROM public.notification_delivery_attempts
            WHERE error_code='worker_lease_expired_commit_unknown'
            """
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError("y07 downgrade refuses loss of crash-recovery ambiguity evidence")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION IF EXISTS {_NEW}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD} TO worker_runtime")
    op.execute("RESET ROLE")
