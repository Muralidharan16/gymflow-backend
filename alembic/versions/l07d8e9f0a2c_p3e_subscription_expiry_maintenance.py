"""P3E: bound legacy member-subscription expiry to the maintenance control plane.

Revision ID: l07d8e9f0a2c
Revises: k07d8e9f0a2b
Create Date: 2026-08-15

The legacy scheduled expiry sweep must not receive cross-tenant table authority
through ``worker_runtime``. This revision exposes one bounded SECURITY DEFINER
capability to the already-isolated maintenance role. The capability has no
tenant/status/date input: it can only transition currently-active legacy
subscriptions whose end date is already before the current UTC date, in a
bounded SKIP LOCKED batch.

The maintenance role receives no direct table privilege. ``app_security_owner``
receives only the exact columns required by the function. No RLS change,
BYPASSRLS, ownership escalation, PUBLIC execution, or runtime-role composition
change is introduced.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "l07d8e9f0a2c"
down_revision = "k07d8e9f0a2b"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_TABLE = "public.member_subscriptions"
_FUNCTION_NAME = "expire_legacy_member_subscriptions"
_FUNCTION_SIGNATURE = "app_secure.expire_legacy_member_subscriptions(integer)"
_SELECT_COLUMNS = {"end_date", "id", "status"}
_UPDATE_COLUMNS = {"status", "updated_at"}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E subscription expiry migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(sa.text("""
        SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
               rolcreaterole, rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname IN (:security_owner, :maintenance_role)
    """), {
        "security_owner": _SECURITY_OWNER,
        "maintenance_role": _MAINTENANCE_ROLE,
    }).mappings().all()
    by_name = {item["rolname"]: item for item in roles}
    if set(by_name) != {_SECURITY_OWNER, _MAINTENANCE_ROLE}:
        raise RuntimeError("P3E subscription expiry required roles are missing")
    for role_name, role in by_name.items():
        if any(bool(role[key]) for key in (
            "rolcanlogin", "rolsuper", "rolinherit", "rolcreatedb",
            "rolcreaterole", "rolreplication", "rolbypassrls",
        )):
            raise RuntimeError(
                f"P3E managed role {role_name} violates reduced-role attributes"
            )

    edge = bind.execute(sa.text("""
        SELECT m.admin_option, m.inherit_option, m.set_option
        FROM pg_catalog.pg_auth_members AS m
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = m.member
        WHERE granted.rolname = :granted
          AND member_role.rolname = :member
    """), {
        "granted": _SECURITY_OWNER,
        "member": _MIGRATION_OWNER,
    }).mappings().all()
    if (
        len(edge) != 1
        or edge[0]["admin_option"]
        or edge[0]["inherit_option"]
        or not edge[0]["set_option"]
    ):
        raise RuntimeError(
            "migration_owner -> app_security_owner must remain SET-only"
        )


def _relation_state(bind):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               relation.relrowsecurity,
               relation.relforcerowsecurity
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND relation.relname = 'member_subscriptions'
          AND relation.relkind IN ('r', 'p')
    """)).mappings().one_or_none()


def _direct_table_privileges(bind, role_name: str) -> set[str]:
    return {
        str(value)
        for value in bind.execute(sa.text("""
            SELECT DISTINCT acl.privilege_type::text
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault('r', relation.relowner)
                )
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            WHERE relation.oid = pg_catalog.to_regclass(:relation)
              AND grantee.rolname = :role_name
        """), {
            "relation": _TABLE,
            "role_name": role_name,
        }).scalars().all()
    }


def _column_privileges(bind, role_name: str, privilege: str) -> set[str]:
    return {
        str(value)
        for value in bind.execute(sa.text("""
            SELECT column_name::text
            FROM information_schema.column_privileges
            WHERE table_schema = 'public'
              AND table_name = 'member_subscriptions'
              AND grantee = :role_name
              AND privilege_type = :privilege
            ORDER BY column_name
        """), {
            "role_name": role_name,
            "privilege": privilege,
        }).scalars().all()
    }


def _function_row(bind):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               procedure.prosrc::text AS source,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )
                   ) AS acl
                   JOIN pg_catalog.pg_roles AS grantee
                     ON grantee.oid = acl.grantee
                   WHERE grantee.rolname = :maintenance_role
                     AND acl.privilege_type = 'EXECUTE'
               ) AS maintenance_execute,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )
                   ) AS acl
                   WHERE acl.grantee = 0
                     AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = :function_name
          AND procedure.pronargs = 1
          AND procedure.prokind = 'f'
    """), {
        "function_name": _FUNCTION_NAME,
        "maintenance_role": _MAINTENANCE_ROLE,
    }).mappings().one_or_none()


def _require_relation_contract(bind) -> None:
    state = _relation_state(bind)
    if state is None or state["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError("legacy member_subscriptions ownership contract drifted")
    if bool(state["relrowsecurity"]) or bool(state["relforcerowsecurity"]):
        raise RuntimeError(
            "P3E does not change the predecessor legacy member_subscriptions RLS state"
        )


def _require_no_direct_background_table_authority(bind) -> None:
    for role_name in (_MAINTENANCE_ROLE, "worker_runtime", "auth_runtime"):
        if _direct_table_privileges(bind, role_name):
            raise RuntimeError(
                f"{role_name} unexpectedly has direct member_subscriptions table ACL"
            )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            if _column_privileges(bind, role_name, privilege):
                raise RuntimeError(
                    f"{role_name} unexpectedly has direct {privilege} columns "
                    "on member_subscriptions"
                )


def _require_predecessor(bind) -> None:
    _require_relation_contract(bind)
    _require_no_direct_background_table_authority(bind)
    if _function_row(bind) is not None:
        raise RuntimeError("P3E subscription expiry capability already exists")
    if _direct_table_privileges(bind, _SECURITY_OWNER):
        raise RuntimeError(
            "app_security_owner unexpectedly has table-wide member_subscriptions ACL"
        )
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        if _column_privileges(bind, _SECURITY_OWNER, privilege):
            raise RuntimeError(
                "app_security_owner predecessor member_subscriptions column ACL drift"
            )
    if not bool(_scalar(
        bind,
        "SELECT pg_catalog.has_schema_privilege(:role_name, 'app_secure', 'USAGE')",
        {"role_name": _MAINTENANCE_ROLE},
    )):
        raise RuntimeError(
            "P3E requires the already-certified maintenance app_secure schema boundary"
        )


def _require_forward(bind) -> None:
    _require_relation_contract(bind)
    _require_no_direct_background_table_authority(bind)
    if _direct_table_privileges(bind, _SECURITY_OWNER):
        raise RuntimeError(
            "app_security_owner leaked table-wide member_subscriptions ACL"
        )
    if _column_privileges(bind, _SECURITY_OWNER, "SELECT") != _SELECT_COLUMNS:
        raise RuntimeError("P3E subscription expiry SELECT column boundary drifted")
    if _column_privileges(bind, _SECURITY_OWNER, "UPDATE") != _UPDATE_COLUMNS:
        raise RuntimeError("P3E subscription expiry UPDATE column boundary drifted")
    for privilege in ("INSERT", "DELETE"):
        if _column_privileges(bind, _SECURITY_OWNER, privilege):
            raise RuntimeError(
                f"app_security_owner unexpectedly has member_subscriptions {privilege}"
            )

    row = _function_row(bind)
    if row is None:
        raise RuntimeError("P3E subscription expiry capability is missing")
    if (
        row["owner_name"] != _SECURITY_OWNER
        or not bool(row["prosecdef"])
        or row["volatility"] != "v"
    ):
        raise RuntimeError("P3E subscription expiry function owner/security drifted")
    if set(row["proconfig"] or []) != {
        "search_path=pg_catalog",
        "row_security=on",
    }:
        raise RuntimeError("P3E subscription expiry function settings drifted")
    if not bool(row["maintenance_execute"]) or bool(row["public_execute"]):
        raise RuntimeError("P3E subscription expiry function EXECUTE ACL drifted")

    source = " ".join(str(row["source"] or "").lower().split())
    for token in (
        "app.internal_maintenance",
        "is distinct from 'platform'",
        "p_batch_size < 1",
        "p_batch_size > 1000",
        "status = 'active'::public.subscriptionstatus",
        "status = 'expired'::public.subscriptionstatus",
        "for update skip locked",
        "at time zone 'utc'",
    ):
        if token not in source:
            raise RuntimeError(
                f"P3E subscription expiry function lost required token: {token}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("""
        GRANT SELECT (id, end_date, status),
              UPDATE (status, updated_at)
        ON TABLE public.member_subscriptions
        TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.expire_legacy_member_subscriptions(
            p_batch_size integer
        ) RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_count integer := 0;
        BEGIN
            IF pg_catalog.current_setting(
                   'app.internal_maintenance', true
               ) IS DISTINCT FROM 'platform'
               OR p_batch_size < 1
               OR p_batch_size > 1000 THEN
                RAISE EXCEPTION 'invalid legacy subscription expiry command'
                    USING ERRCODE = '42501';
            END IF;

            WITH target AS (
                SELECT subscription.id
                FROM public.member_subscriptions AS subscription
                WHERE subscription.status = 'active'::public.subscriptionstatus
                  AND subscription.end_date
                      < (
                          pg_catalog.clock_timestamp()
                          AT TIME ZONE 'UTC'
                        )::date
                ORDER BY subscription.end_date, subscription.id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE public.member_subscriptions AS subscription
            SET status = 'expired'::public.subscriptionstatus,
                updated_at = pg_catalog.clock_timestamp()
            FROM target
            WHERE subscription.id = target.id
              AND subscription.status = 'active'::public.subscriptionstatus
              AND subscription.end_date
                  < (
                      pg_catalog.clock_timestamp()
                      AT TIME ZONE 'UTC'
                    )::date;

            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$;
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE} TO {_MAINTENANCE_ROLE}"
    )
    op.execute("RESET ROLE")

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION {_FUNCTION_SIGNATURE}")
    op.execute("RESET ROLE")

    op.execute("""
        REVOKE UPDATE (status, updated_at),
               SELECT (id, end_date, status)
        ON TABLE public.member_subscriptions
        FROM app_security_owner
    """)

    _require_predecessor(bind)
