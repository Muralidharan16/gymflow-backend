"""Expose the aggregate lifecycle-saga dead-letter count to maintenance.

Revision ID: zk07d8e9f0a45
Revises: zj07d8e9f0a44
Create Date: 2026-09-15

P8-O proved that lifecycle observability must not depend on direct maintenance
SELECT access to the durable branch outbox.  The dedicated lifecycle maintenance
identity intentionally has no raw outbox authority, while the previously
certified app_security_owner already owns the bounded read authority consumed by
P4E aggregate snapshots.

This revision adds one no-argument SECURITY DEFINER function returning only the
count of dead-lettered ``branch.lifecycle_saga`` commands.  It exposes no tenant,
branch, command, payload or correlation identifiers; performs no mutation; adds
no table privilege; and grants EXECUTE only to lifecycle_maintenance_runtime.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zk07d8e9f0a45"
down_revision = "zj07d8e9f0a44"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_OUTBOX = "public.branch_outbox_events"
_SCHEMA = "app_secure"
_FUNCTION_NAME = "lifecycle_saga_dead_letter_count"
_SIGNATURE = f"{_SCHEMA}.{_FUNCTION_NAME}()"


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if (
        row["session_name"] != _MIGRATION_OWNER
        or row["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("zk07 P8 lifecycle snapshot requires migration_owner")
    if any(
        bool(row[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("zk07 migration_owner violates reduced-role contract")


def _function_oid(bind):
    return bind.execute(
        sa.text(
            """
            SELECT p.oid
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = :schema_name
              AND p.proname = :function_name
              AND p.pronargs = 0
            """
        ),
        {"schema_name": _SCHEMA, "function_name": _FUNCTION_NAME},
    ).scalar_one_or_none()


def _column_select_count(bind, role_name: str) -> int:
    return int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'branch_outbox_events'
                  AND grantee = :role_name
                  AND privilege_type = 'SELECT'
                """
            ),
            {"role_name": role_name},
        ).scalar_one()
    )


def _require_predecessor(bind) -> None:
    if _function_oid(bind) is not None:
        raise RuntimeError(f"zk07 function unexpectedly exists: {_SIGNATURE}")

    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role, :relation, 'SELECT')"
            ),
            {"role": _SECURITY_OWNER, "relation": _OUTBOX},
        ).scalar_one()
    ):
        raise RuntimeError(
            "zk07 predecessor lacks certified app_security_owner outbox SELECT"
        )

    # The whole point of this capability is to keep raw outbox data away from
    # the maintenance identity. Refuse to install over an already-broadened ACL.
    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role, :relation, 'SELECT')"
            ),
            {"role": _MAINTENANCE, "relation": _OUTBOX},
        ).scalar_one()
    ) or _column_select_count(bind, _MAINTENANCE):
        raise RuntimeError("zk07 refuses pre-existing maintenance raw outbox SELECT")


def _install() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        r"""
        CREATE FUNCTION app_secure.lifecycle_saga_dead_letter_count()
        RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public
        SET row_security=on
        AS $function$
            SELECT count(*)::bigint
            FROM public.branch_outbox_events AS o
            WHERE o.event_type = 'branch.lifecycle_saga'
              AND o.status = 'dead_lettered'
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "app_secure.lifecycle_saga_dead_letter_count() FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.lifecycle_saga_dead_letter_count() "
        "TO lifecycle_maintenance_runtime"
    )
    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT p.oid, owner.rolname AS owner_name, p.prosecdef,
                   p.provolatile::text AS volatility, p.proconfig
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
            WHERE n.nspname = :schema_name
              AND p.proname = :function_name
              AND p.pronargs = 0
            """
        ),
        {"schema_name": _SCHEMA, "function_name": _FUNCTION_NAME},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"zk07 installed function missing: {_SIGNATURE}")
    if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
        raise RuntimeError("zk07 lifecycle snapshot owner/security drift")
    if row["volatility"] != "s":
        raise RuntimeError("zk07 lifecycle snapshot must remain STABLE")

    config = set(row["proconfig"] or ())
    if "row_security=on" not in config or not any(
        value.startswith("search_path=") for value in config
    ):
        raise RuntimeError("zk07 lifecycle snapshot lost hardened function config")

    function_oid = int(row["oid"])
    if not bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_function_privilege(
                    :role, CAST(:oid AS oid), 'EXECUTE'
                )
                """
            ),
            {"role": _MAINTENANCE, "oid": function_oid},
        ).scalar_one()
    ):
        raise RuntimeError("zk07 maintenance EXECUTE missing")

    public_execute = bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_proc AS p
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                    ) AS acl
                    WHERE p.oid = CAST(:oid AS oid)
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
                )
                """
            ),
            {"oid": function_oid},
        ).scalar_one()
    )
    if public_execute:
        raise RuntimeError("zk07 unexpected PUBLIC EXECUTE")

    for blocked_role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "finance_config_runtime",
    ):
        if bool(
            bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.has_function_privilege(
                        :role, CAST(:oid AS oid), 'EXECUTE'
                    )
                    """
                ),
                {"role": blocked_role, "oid": function_oid},
            ).scalar_one()
        ):
            raise RuntimeError(
                f"zk07 unexpected lifecycle snapshot EXECUTE for {blocked_role}"
            )

    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role, :relation, 'SELECT')"
            ),
            {"role": _MAINTENANCE, "relation": _OUTBOX},
        ).scalar_one()
    ) or _column_select_count(bind, _MAINTENANCE):
        raise RuntimeError("zk07 leaked raw outbox SELECT to maintenance")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_predecessor(bind)
    _install()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    if _function_oid(bind) is None:
        raise RuntimeError(f"zk07 downgrade refuses missing function: {_SIGNATURE}")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("DROP FUNCTION app_secure.lifecycle_saga_dead_letter_count()")
    op.execute("RESET ROLE")
