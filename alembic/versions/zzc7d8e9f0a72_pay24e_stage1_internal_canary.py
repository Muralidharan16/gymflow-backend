"""PAY-24-E Stage-1 internal-canary entitlement authority fence.

Revision ID: zzc7d8e9f0a72
Revises: zzb7d8e9f0a71
Create Date: 2026-10-03

This revision does not activate Stage 1. It binds entitlement claiming and
protected mutation to the durable PAY-24-A Stage-1 internal-canary authority.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zzc7d8e9f0a72"
down_revision = "zzb7d8e9f0a71"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_ENTITLEMENT = "entitlement_runtime"
_OLD_CLAIM = "app_secure.pay24c_claim_entitlement_commands(uuid,integer,integer)"
_NEW_CLAIM = "app_secure.pay24e_claim_entitlement_commands(uuid,integer,integer)"
_ASSERT = "app_secure.pay24e_assert_stage1_entitlement_authority(uuid)"


def _preflight(bind) -> None:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-24-E migration requires migration_owner")

    head = bind.execute(
        sa.text("SELECT version_num::text FROM alembic_version")
    ).scalar_one()
    if head != down_revision:
        raise RuntimeError(
            f"PAY-24-E predecessor drift: expected {down_revision}, found {head}"
        )

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege("
            "'app_security_owner','finance.payment_activation_authority','SELECT')"
        )
    ).scalar_one():
        raise RuntimeError(
            "PAY-24-E security owner cannot inspect durable activation authority"
        )

    collision = bind.execute(
        sa.text(
            """
            SELECT p.proname
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname IN (
                    'pay24e_assert_stage1_entitlement_authority',
                    'pay24e_claim_entitlement_commands'
              )
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if collision is not None:
        raise RuntimeError(
            "PAY-24-E unexpected pre-existing function: app_secure." + collision
        )


def _install() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24e_assert_stage1_entitlement_authority(
                p_organization_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            STABLE
            SECURITY DEFINER
            SET search_path=pg_catalog,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'entitlement_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-E entitlement authority requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_organization_id IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-24-E entitlement organization is required'
                        USING ERRCODE='22023';
                END IF;

                SELECT a.* INTO STRICT v_authority
                FROM finance.payment_activation_authority a
                WHERE a.singleton;

                IF v_authority.stage<>1
                   OR v_authority.provider_egress_state<>'open'
                   OR v_authority.internal_organization_id
                        IS DISTINCT FROM p_organization_id
                   OR v_authority.checkout IS NOT TRUE
                   OR v_authority.webhooks IS NOT TRUE
                   OR v_authority.payment_application IS NOT TRUE
                   OR v_authority.subscription_activation IS NOT TRUE
                   OR v_authority.refund_execution IS NOT FALSE
                   OR v_authority.recurring_billing IS NOT FALSE
                   OR v_authority.dunning IS NOT FALSE
                   OR v_authority.platform_billing IS NOT FALSE
                   OR v_authority.release_identity_id IS NULL
                   OR v_authority.authorization_record_id IS NULL
                   OR v_authority.authorization_id IS NULL
                   OR v_authority.authorized_stage IS DISTINCT FROM 1
                   OR v_authority.certified_sha IS NULL
                   OR v_authority.deployed_sha
                        IS DISTINCT FROM v_authority.certified_sha
                   OR v_authority.authorized_sha
                        IS DISTINCT FROM v_authority.certified_sha
                THEN
                    RAISE EXCEPTION
                        'PAY-24-E Stage-1 entitlement authority denied'
                        USING ERRCODE='42501';
                END IF;
            END
            $function$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {_ASSERT} FROM PUBLIC")

        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24e_claim_entitlement_commands(
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
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_authority finance.payment_activation_authority%ROWTYPE;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,'entitlement_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-E command claim requires entitlement_runtime'
                        USING ERRCODE='42501';
                END IF;
                IF p_worker_id IS NULL
                   OR p_batch_size IS NULL OR p_batch_size<1 OR p_batch_size>100
                   OR p_lease_seconds IS NULL OR p_lease_seconds<30
                   OR p_lease_seconds>3600
                THEN
                    RAISE EXCEPTION
                        'PAY-24-E command claim arguments invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT a.* INTO STRICT v_authority
                FROM finance.payment_activation_authority a
                WHERE a.singleton;

                IF v_authority.stage=0 THEN
                    RETURN;
                END IF;

                PERFORM app_secure.pay24e_assert_stage1_entitlement_authority(
                    v_authority.internal_organization_id
                );

                PERFORM pg_catalog.set_config(
                    'app.internal_maintenance',
                    'pay24c_entitlement_claim',true
                );

                RETURN QUERY
                WITH candidates AS (
                    SELECT c.command_id,
                           (c.status='processing') AS reclaiming
                    FROM public.member_entitlement_commands c
                    WHERE c.organization_id=v_authority.internal_organization_id
                      AND (
                        (
                            c.status='pending'
                            AND c.attempt_count<c.max_attempts
                        ) OR (
                            c.status='processing'
                            AND c.leased_until<=pg_catalog.clock_timestamp()
                        )
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
        op.execute(f"REVOKE ALL ON FUNCTION {_NEW_CLAIM} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_CLAIM} TO {_ENTITLEMENT}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {_OLD_CLAIM} FROM {_ENTITLEMENT}")

        guards = (
            (
                "pay24c_guard_term_mutation",
                "PAY-24-C protected term mutation requires entitlement_runtime",
                "RETURN NEW;",
                "RETURN NEW;",
            ),
            (
                "pay24c_guard_v2_mutation",
                "PAY-24-C protected V2 mutation requires entitlement_runtime",
                "RETURN NEW;",
                "RETURN NEW;",
            ),
            (
                "pay24c_guard_freeze_mutation",
                "PAY-24-C freeze mutation requires entitlement_runtime",
                "RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;",
                "RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;",
            ),
        )
        for name, identity_error, migration_return, final_return in guards:
            op.execute(
                f"""
                CREATE OR REPLACE FUNCTION app_secure.{name}()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path=pg_catalog,public,finance
                AS $function$
                DECLARE
                    v_org uuid;
                BEGIN
                    IF session_user='migration_owner' THEN
                        {migration_return}
                    END IF;
                    IF NOT (
                        current_user='app_security_owner'
                        AND pg_catalog.pg_has_role(
                            session_user,'entitlement_runtime','MEMBER'
                        )
                    ) THEN
                        RAISE EXCEPTION
                            '{identity_error}'
                            USING ERRCODE='42501';
                    END IF;
                    v_org:=NULLIF(
                        pg_catalog.current_setting('app.current_org_id',true),''
                    )::uuid;
                    IF v_org IS NULL THEN
                        RAISE EXCEPTION
                            'PAY-24-E entitlement tenant context required'
                            USING ERRCODE='42501';
                    END IF;
                    PERFORM app_secure.pay24e_assert_stage1_entitlement_authority(
                        v_org
                    );
                    {final_return}
                END
                $function$
                """
            )
    finally:
        op.execute("RESET ROLE")


def _prove(bind) -> None:
    for name in (
        "pay24e_assert_stage1_entitlement_authority",
        "pay24e_claim_entitlement_commands",
    ):
        row = bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_get_userbyid(p.proowner),p.prosecdef,p.oid
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure' AND p.proname=:name
                """
            ),
            {"name": name},
        ).one()
        if row[0] != _SECURITY_OWNER or row[1] is not True:
            raise RuntimeError(f"PAY-24-E function owner/definer drift: {name}")

    old_oid = bind.execute(
        sa.text(
            """
            SELECT p.oid FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='pay24c_claim_entitlement_commands'
              AND p.pronargs=3
            """
        )
    ).scalar_one()
    new_oid = bind.execute(
        sa.text(
            """
            SELECT p.oid FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='app_secure'
              AND p.proname='pay24e_claim_entitlement_commands'
              AND p.pronargs=3
            """
        )
    ).scalar_one()
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_function_privilege("
            ":role,CAST(:oid AS oid),'EXECUTE')"
        ),
        {"role": _ENTITLEMENT, "oid": int(old_oid)},
    ).scalar_one():
        raise RuntimeError("PAY-24-E predecessor claim remains reachable")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_function_privilege("
            ":role,CAST(:oid AS oid),'EXECUTE')"
        ),
        {"role": _ENTITLEMENT, "oid": int(new_oid)},
    ).scalar_one():
        raise RuntimeError("PAY-24-E new claim is not reachable")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _preflight(bind)
    _install()
    _prove(bind)


def downgrade() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {_NEW_CLAIM} FROM {_ENTITLEMENT}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_CLAIM} TO {_ENTITLEMENT}")

        op.execute(
            r"""
            CREATE OR REPLACE FUNCTION app_secure.pay24c_guard_term_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN RETURN NEW; END IF;
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
            CREATE OR REPLACE FUNCTION app_secure.pay24c_guard_v2_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user='migration_owner' THEN RETURN NEW; END IF;
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
            CREATE OR REPLACE FUNCTION app_secure.pay24c_guard_freeze_mutation()
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
        op.execute(f"DROP FUNCTION {_NEW_CLAIM}")
        op.execute(f"DROP FUNCTION {_ASSERT}")
    finally:
        op.execute("RESET ROLE")
