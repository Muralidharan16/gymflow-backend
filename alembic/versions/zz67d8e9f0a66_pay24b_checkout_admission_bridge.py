"""PAY-24-B checkout provider-admission bridge.

Revision ID: zz67d8e9f0a66
Revises: zz57d8e9f0a65
Create Date: 2026-09-26

Expose one bounded checkout-only bridge for finance_payment_runtime.  The bridge
reads the current durable activation generation under app_security_owner and
delegates all Stage/egress/tenant/release/capability/idempotency checks to the
certified PAY-24-A admission function.  It performs no provider I/O.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz67d8e9f0a66"
down_revision = "zz57d8e9f0a65"
branch_labels = None
depends_on = None

_FUNCTION = (
    "app_secure.pay24b_request_current_provider_admission(text,text,text,integer)"
)
_FUNCTION_NAME = "pay24b_request_current_provider_admission"
_FUNCTION_ARGTYPES = "text, text, text, integer"
_PREDECESSOR_NAME = "pay24a_request_provider_admission"
_PREDECESSOR_ARGTYPES = "bigint, text, text, text, integer"


def _catalog_function_exists(
    bind,
    *,
    schema: str,
    name: str,
    argtypes: str,
) -> bool:
    """Inspect exact function identity without requiring schema USAGE."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS("
                "SELECT 1 "
                "FROM pg_catalog.pg_proc AS p "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace "
                "WHERE n.nspname=:schema "
                "AND p.proname=:name "
                "AND pg_catalog.oidvectortypes(p.proargtypes)=:argtypes)"
            ),
            {"schema": schema, "name": name, "argtypes": argtypes},
        ).scalar_one()
    )


def _preflight(bind) -> None:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != ("migration_owner", "migration_owner"):
        raise RuntimeError("PAY-24-B migration requires migration_owner")

    for role in ("app_security_owner", "finance_payment_runtime"):
        if not bind.execute(
            sa.text(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:role)"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(f"PAY-24-B missing externally managed role: {role}")

    if not _catalog_function_exists(
        bind,
        schema="app_secure",
        name=_PREDECESSOR_NAME,
        argtypes=_PREDECESSOR_ARGTYPES,
    ):
        raise RuntimeError("PAY-24-B PAY-24-A admission predecessor missing")

    if _catalog_function_exists(
        bind,
        schema="app_secure",
        name=_FUNCTION_NAME,
        argtypes=_FUNCTION_ARGTYPES,
    ):
        raise RuntimeError("PAY-24-B checkout admission bridge already exists")


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay24b_request_current_provider_admission(
                p_capability text,
                p_logical_operation_id text,
                p_operation_sha text,
                p_lease_seconds integer
            )
            RETURNS TABLE(
                admission_id uuid,
                activation_generation bigint,
                organization_id uuid,
                capability text,
                logical_operation_id text,
                lease_expires_at timestamptz,
                state text
            )
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path=pg_catalog
            SET row_security=on
            AS $function$
            DECLARE
                v_generation bigint;
            BEGIN
                IF p_capability IS DISTINCT FROM 'checkout' THEN
                    RAISE EXCEPTION
                        'PAY-24-B bridge permits checkout admission only'
                        USING ERRCODE='42501';
                END IF;
                IF NOT pg_catalog.pg_has_role(
                    session_user,'finance_payment_runtime','MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'PAY-24-B checkout admission requires finance_payment_runtime'
                        USING ERRCODE='42501';
                END IF;

                SELECT a.generation
                  INTO STRICT v_generation
                  FROM finance.payment_activation_authority AS a
                 WHERE a.singleton;

                RETURN QUERY
                SELECT requested.admission_id,
                       requested.activation_generation,
                       requested.organization_id,
                       requested.capability,
                       requested.logical_operation_id,
                       requested.lease_expires_at,
                       requested.state
                  FROM app_secure.pay24a_request_provider_admission(
                       v_generation,
                       p_capability,
                       p_logical_operation_id,
                       p_operation_sha,
                       p_lease_seconds
                  ) AS requested;
            END
            $function$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO finance_payment_runtime"
        )
    finally:
        op.execute("RESET ROLE")


def downgrade() -> None:
    bind = op.get_bind()
    if not _catalog_function_exists(
        bind,
        schema="app_secure",
        name=_FUNCTION_NAME,
        argtypes=_FUNCTION_ARGTYPES,
    ):
        raise RuntimeError("PAY-24-B checkout admission bridge is missing")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} FROM finance_payment_runtime"
        )
        op.execute(f"DROP FUNCTION {_FUNCTION}")
    finally:
        op.execute("RESET ROLE")
