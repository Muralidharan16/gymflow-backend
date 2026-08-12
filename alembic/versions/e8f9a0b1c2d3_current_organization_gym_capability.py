"""Expose only current-tenant legacy gym ownership to app_runtime.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-12

Legacy gym-scoped routes must prove that a requested gym belongs to the
verified request tenant. The gyms table contains broader legacy data that
ordinary API runtime does not otherwise need. This revision therefore exposes
one boolean SECURITY DEFINER capability bound to app.current_org_id instead of
granting app_runtime base-table SELECT.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_FUNCTION = "public.current_organization_owns_gym(uuid)"

_CREATE_FUNCTION = """
CREATE FUNCTION public.current_organization_owns_gym(target_gym_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = on
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.gyms AS gym
        WHERE gym.id = target_gym_id
          AND gym.org_id = NULLIF(
              current_setting('app.current_org_id'::text, true),
              ''
          )::uuid
    )
$function$
"""


def _bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("e8f9 current-org gym capability requires online catalog access")
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable")
    return bind


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   role_data.rolsuper,
                   role_data.rolinherit,
                   role_data.rolcreatedb,
                   role_data.rolcreaterole,
                   role_data.rolreplication,
                   role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("e8f9 requires session_user=current_user=migration_owner")
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
        raise RuntimeError("migration_owner violates the reduced migration contract")

    security = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).mappings().one_or_none()
    if security is None or any(bool(value) for value in security.values()):
        raise RuntimeError("app_security_owner must remain reduced NOLOGIN/NOBYPASSRLS")
    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner lacks bounded SET capability to app_security_owner")


def _direct_gym_column_privileges(bind, role_name: str) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.text(
                """
                SELECT column_name, privilege_type
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'gyms'
                  AND grantee = :role_name
                ORDER BY column_name, privilege_type
                """
            ),
            {"role_name": role_name},
        ).all()
    }


def _function_row(bind):
    return bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text AS owner_name,
                   procedure_data.prosecdef,
                   procedure_data.provolatile::text AS volatility,
                   procedure_data.proconfig,
                   pg_catalog.pg_get_function_result(procedure_data.oid)::text AS result_type,
                   pg_catalog.pg_get_function_identity_arguments(procedure_data.oid)::text AS identity_arguments,
                   procedure_data.prosrc::text AS source
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = procedure_data.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE namespace_data.nspname = 'public'
              AND procedure_data.proname = 'current_organization_owns_gym'
              AND procedure_data.pronargs = 1
            """
        )
    ).mappings().one_or_none()


def _predecessor(bind) -> None:
    owner = _scalar(
        bind,
        """
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)::text
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'public.gyms'::regclass
        """,
    )
    if owner != _MIGRATION_OWNER:
        raise RuntimeError(f"unexpected gyms owner before e8f9: {owner!r}")
    if _function_row(bind) is not None:
        raise RuntimeError("current_organization_owns_gym already exists before e8f9")
    if _direct_gym_column_privileges(bind, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner already has gyms column ACLs")
    if _scalar(
        bind,
        "SELECT pg_catalog.has_table_privilege(:role_name, 'public.gyms', 'SELECT')",
        {"role_name": _API},
    ):
        raise RuntimeError("app_runtime unexpectedly has broad gyms SELECT")


def _public_create(bind, role_name: str) -> bool:
    return bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(:role_name, 'public', 'CREATE')",
            {"role_name": role_name},
        )
    )


def _run_as_security_owner(bind, sql: str) -> None:
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    if _scalar(bind, "SELECT current_user::text") != _SECURITY_OWNER:
        raise RuntimeError("failed to enter app_security_owner")
    bind.exec_driver_sql(sql)
    bind.execute(sa.text("RESET ROLE"))
    if _scalar(bind, "SELECT current_user::text") != _MIGRATION_OWNER:
        raise RuntimeError("failed to restore migration_owner")


def _verify_forward(bind) -> None:
    row = _function_row(bind)
    if row is None:
        raise RuntimeError("current_organization_owns_gym missing after upgrade")
    if row["owner_name"] != _SECURITY_OWNER or not bool(row["prosecdef"]):
        raise RuntimeError("current_organization_owns_gym owner/security-definer drift")
    if row["volatility"] != "s" or row["identity_arguments"] != "uuid" or row["result_type"] != "boolean":
        raise RuntimeError("current_organization_owns_gym signature/volatility drift")
    configs = set(row["proconfig"] or [])
    if configs != {"search_path=pg_catalog", "row_security=on"}:
        raise RuntimeError(f"current_organization_owns_gym proconfig drift: {configs!r}")
    source = " ".join(str(row["source"]).split()).lower()
    for token in ("public.gyms", "gym.id", "gym.org_id", "target_gym_id", "app.current_org_id"):
        if token not in source:
            raise RuntimeError(f"current_organization_owns_gym source drift: missing {token}")

    expected_columns = {("id", "SELECT"), ("org_id", "SELECT")}
    if _direct_gym_column_privileges(bind, _SECURITY_OWNER) != expected_columns:
        raise RuntimeError("app_security_owner gyms column ACL drift")

    if not _scalar(
        bind,
        "SELECT pg_catalog.has_function_privilege(:role_name, :function_name, 'EXECUTE')",
        {"role_name": _API, "function_name": _FUNCTION},
    ):
        raise RuntimeError("app_runtime lacks current_organization_owns_gym EXECUTE")
    if _scalar(
        bind,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = procedure_data.pronamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure_data.proacl,
                    pg_catalog.acldefault('f', procedure_data.proowner)
                )
            ) AS function_acl
            WHERE namespace_data.nspname = 'public'
              AND procedure_data.proname = 'current_organization_owns_gym'
              AND procedure_data.pronargs = 1
              AND function_acl.grantee = 0
              AND function_acl.privilege_type = 'EXECUTE'
        )
        """,
    ):
        raise RuntimeError("PUBLIC must not execute current_organization_owns_gym")
    if _scalar(
        bind,
        "SELECT pg_catalog.has_table_privilege(:role_name, 'public.gyms', 'SELECT')",
        {"role_name": _API},
    ):
        raise RuntimeError("app_runtime must not gain broad gyms SELECT")
    for column_name in ("name", "gymu_id", "address", "phone"):
        if _scalar(
            bind,
            "SELECT pg_catalog.has_column_privilege(:role_name, 'public.gyms', :column_name, 'SELECT')",
            {"role_name": _API, "column_name": column_name},
        ):
            raise RuntimeError(f"app_runtime gained forbidden gym column read: {column_name}")


def upgrade() -> None:
    bind = _bind()
    _identity(bind)
    _predecessor(bind)

    op.execute("GRANT SELECT (id, org_id) ON TABLE public.gyms TO app_security_owner")

    had_create = _public_create(bind, _SECURITY_OWNER)
    if had_create:
        raise RuntimeError("app_security_owner unexpectedly has CREATE on public before e8f9")
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    try:
        _run_as_security_owner(bind, _CREATE_FUNCTION)
    finally:
        op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")
    if _public_create(bind, _SECURITY_OWNER):
        raise RuntimeError("temporary app_security_owner CREATE on public was not restored")

    _run_as_security_owner(
        bind,
        "REVOKE ALL ON FUNCTION public.current_organization_owns_gym(uuid) FROM PUBLIC",
    )
    _run_as_security_owner(
        bind,
        "GRANT EXECUTE ON FUNCTION public.current_organization_owns_gym(uuid) TO app_runtime",
    )
    _verify_forward(bind)


def downgrade() -> None:
    bind = _bind()
    _identity(bind)
    _verify_forward(bind)

    _run_as_security_owner(
        bind,
        "DROP FUNCTION public.current_organization_owns_gym(uuid) RESTRICT",
    )
    op.execute("REVOKE SELECT (id, org_id) ON TABLE public.gyms FROM app_security_owner")
    _predecessor(bind)
