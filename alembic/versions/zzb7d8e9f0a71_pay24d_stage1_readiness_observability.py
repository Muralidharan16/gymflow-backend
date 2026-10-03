"""PAY-24-D Stage-1 readiness observability.

Revision ID: zzb7d8e9f0a71
Revises: zza7d8e9f0a70
Create Date: 2026-10-03

This migration adds only a bounded aggregate readiness snapshot for the
PAY-24-C entitlement command journal. It does not schedule a worker, transition
PAY-24-A, open provider egress, enable a kill switch, or authorize money movement.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zzb7d8e9f0a71"
down_revision = "zza7d8e9f0a70"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_FUNCTION = "app_secure.pay24d_entitlement_readiness_snapshot()"
_ALLOWED = (
    "entitlement_runtime",
    "finance_config_runtime",
    "finance_read_runtime",
    "lifecycle_maintenance_runtime",
)
_DENIED = (
    "app_runtime",
    "app_user",
    "worker_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
)


def _require_reduced_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=:role
            """
        ),
        {"role": role},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-24-D missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-24-D role login posture drift: {role}")
    for field in (
        "rolsuper","rolinherit","rolcreatedb","rolcreaterole",
        "rolreplication","rolbypassrls",
    ):
        if bool(row[field]):
            raise RuntimeError(f"PAY-24-D reduced-role drift: {role}.{field}")


def _preflight(bind) -> None:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-24-D migration requires migration_owner")
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    for role in (*_ALLOWED, *_DENIED):
        _require_reduced_role(bind, role)
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
        {"signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("PAY-24-D readiness snapshot already exists")


def _install() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24d_entitlement_readiness_snapshot()
            RETURNS TABLE(
                pending_count bigint,
                processing_count bigint,
                failed_count bigint,
                review_required_count bigint,
                oldest_pending_age_seconds bigint,
                expired_processing_leases bigint
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog,public
            SET row_security=on
            AS $function$
            BEGIN
                IF NOT (
                    pg_catalog.pg_has_role(
                        session_user,'entitlement_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_config_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'finance_read_runtime','MEMBER'
                    )
                    OR pg_catalog.pg_has_role(
                        session_user,'lifecycle_maintenance_runtime','MEMBER'
                    )
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-D readiness snapshot role denied'
                        USING ERRCODE='42501';
                END IF;

                PERFORM pg_catalog.set_config(
                    'app.internal_maintenance',
                    'pay24c_entitlement_claim',
                    true
                );

                RETURN QUERY
                SELECT
                    count(*) FILTER (WHERE c.status='pending')::bigint,
                    count(*) FILTER (WHERE c.status='processing')::bigint,
                    count(*) FILTER (WHERE c.status='failed')::bigint,
                    count(*) FILTER (WHERE c.status='review_required')::bigint,
                    COALESCE(
                        GREATEST(
                            0,
                            EXTRACT(
                                EPOCH FROM (
                                    pg_catalog.clock_timestamp()
                                    - min(c.created_at)
                                      FILTER (WHERE c.status='pending')
                                )
                            )::bigint
                        ),
                        0
                    )::bigint,
                    count(*) FILTER (
                        WHERE c.status='processing'
                          AND c.leased_until<=pg_catalog.clock_timestamp()
                    )::bigint
                FROM public.member_entitlement_commands c;
            END
            $function$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
        for role in _ALLOWED:
            op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO {role}")
    finally:
        op.execute("RESET ROLE")


def _prove(bind) -> None:
    owner = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(p.proowner)
            FROM pg_catalog.pg_proc p
            WHERE p.oid=pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": _FUNCTION},
    ).scalar_one()
    if owner != _SECURITY_OWNER:
        raise RuntimeError("PAY-24-D readiness function owner drift")

    for role in _ALLOWED:
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role,pg_catalog.to_regprocedure(:signature),'EXECUTE')"
            ),
            {"role": role, "signature": _FUNCTION},
        ).scalar_one():
            raise RuntimeError(f"PAY-24-D missing readiness EXECUTE: {role}")

    for role in _DENIED:
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role,pg_catalog.to_regprocedure(:signature),'EXECUTE')"
            ),
            {"role": role, "signature": _FUNCTION},
        ).scalar_one():
            raise RuntimeError(f"PAY-24-D leaked readiness EXECUTE: {role}")

    if bind.execute(
        sa.text(
            """
            SELECT EXISTS(
                SELECT 1
                FROM pg_catalog.pg_proc AS p
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        p.proacl,
                        pg_catalog.acldefault('f',p.proowner)
                    )
                ) AS acl
                WHERE p.oid=pg_catalog.to_regprocedure(:signature)
                  AND acl.grantee=0
                  AND acl.privilege_type='EXECUTE'
            )
            """
        ),
        {"signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("PAY-24-D leaked readiness EXECUTE to PUBLIC")

    for role in (*_ALLOWED, *_DENIED):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                ":role,'public.member_entitlement_commands','SELECT')"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(
                f"PAY-24-D leaked direct entitlement journal SELECT: {role}"
            )


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
        op.execute(f"DROP FUNCTION {_FUNCTION}")
    finally:
        op.execute("RESET ROLE")
