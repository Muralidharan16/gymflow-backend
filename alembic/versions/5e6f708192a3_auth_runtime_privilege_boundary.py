"""Establish the database ACL boundary for pre-tenant authentication/bootstrap.

Revision ID: 5e6f708192a3
Revises: 4d5e6f708192
Create Date: 2026-08-09

``auth_runtime`` is a cluster-managed NOLOGIN privilege group. It is provisioned
outside Alembic, while this revision owns only its per-database object grants.
A deployment login may inherit this role together with the ordinary application
privilege groups; the normal application login must not inherit it.

The role receives only the tenant-root/session operations required by the
verified authentication and onboarding flows. It receives no DELETE, TRUNCATE,
DDL, schema ownership, finance access, Platform Billing access, or RLS bypass.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "5e6f708192a3"
down_revision = "4d5e6f708192"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RLS_OWNER = "app_rls_executor"
_AUTH_ROLE = "auth_runtime"

# Direct auth-runtime ACLs are intentionally small. Branch/address/contact RLS
# operations are supplied separately by the ordinary app_runtime/app_user groups
# inherited by the dedicated auth deployment login.
_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "public.organizations": ("SELECT", "INSERT", "UPDATE"),
    "public.owners": ("SELECT", "INSERT", "UPDATE"),
    "public.gyms": ("SELECT", "INSERT", "UPDATE"),
    "public.facility_types": ("SELECT",),
    "public.gym_facility_types": ("SELECT", "INSERT"),
    "public.auth_session_families": ("SELECT", "INSERT", "UPDATE"),
    "public.auth_sessions": ("SELECT", "INSERT", "UPDATE"),
    "public.trial_subscriptions": ("SELECT", "INSERT"),
    "public.audit_logs": ("INSERT",),
}

_FORBIDDEN_TABLE_PRIVILEGES = (
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_role_contract(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name,
                migration.rolsuper,
                migration.rolinherit,
                migration.rolcreatedb,
                migration.rolcreaterole,
                migration.rolreplication,
                migration.rolbypassrls
            FROM pg_catalog.pg_roles AS migration
            WHERE migration.rolname = current_user
            """
        )
    ).mappings().one()

    if identity["session_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError("auth_runtime migration requires session_user=migration_owner")
    if identity["current_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError("auth_runtime migration requires current_user=migration_owner")
    if any(
        bool(identity[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner is over-privileged for auth_runtime migration")

    auth = bind.execute(
        sa.text(
            """
            SELECT
                rolsuper,
                rolinherit,
                rolcreatedb,
                rolcreaterole,
                rolcanlogin,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role
            """
        ),
        {"role": _AUTH_ROLE},
    ).mappings().one_or_none()
    if auth is None:
        raise RuntimeError(
            "Required cluster-managed role auth_runtime is absent; provision it before Alembic"
        )
    if any(bool(auth[key]) for key in auth):
        raise RuntimeError(
            "auth_runtime must be NOLOGIN/NOINHERIT/NOBYPASSRLS and have no elevated role attributes"
        )

    if _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, CAST(:role AS name), 'MEMBER')",
        {"role": _AUTH_ROLE},
    ):
        raise RuntimeError("migration_owner must never be a member of auth_runtime")

    missing_relations = bind.execute(
        sa.text(
            """
            SELECT relation_name
            FROM unnest(CAST(:relations AS text[])) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY relation_name
            """
        ),
        {"relations": list(_TABLE_PRIVILEGES)},
    ).scalars().all()
    if missing_relations:
        raise RuntimeError(
            f"auth_runtime predecessor relations are missing: {tuple(missing_relations)!r}"
        )

    direct_acl_count = _scalar(
        bind,
        """
        SELECT count(*)::bigint
        FROM information_schema.table_privileges
        WHERE grantee = :role
        """,
        {"role": _AUTH_ROLE},
    )
    if direct_acl_count != 0:
        raise RuntimeError(
            "auth_runtime has pre-existing table ACLs; refusing to silently normalize privilege drift"
        )


def _relation_owner(bind, relation: str) -> str:
    owner = _scalar(
        bind,
        """
        SELECT pg_catalog.pg_get_userbyid(class_data.relowner)::text
        FROM pg_catalog.pg_class AS class_data
        WHERE class_data.oid = pg_catalog.to_regclass(:relation)
        """,
        {"relation": relation},
    )
    if owner not in {_MIGRATION_OWNER, _RLS_OWNER}:
        raise RuntimeError(
            f"Unexpected owner for auth_runtime relation {relation}: {owner!r}"
        )
    return str(owner)


def _execute_as_relation_owner(bind, relation: str, sql: str) -> None:
    owner = _relation_owner(bind, relation)
    if owner == _MIGRATION_OWNER:
        bind.execute(sa.text(sql))
        return

    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, CAST(:role AS name), 'SET')",
        {"role": owner},
    ):
        raise RuntimeError(
            f"migration_owner lacks bounded SET capability to relation owner {owner}"
        )

    bind.execute(sa.text(f"SET LOCAL ROLE {owner}"))
    try:
        bind.execute(sa.text(sql))
    finally:
        bind.execute(sa.text("RESET ROLE"))


def _grant_contract(bind) -> None:
    for relation, privileges in _TABLE_PRIVILEGES.items():
        privilege_sql = ", ".join(privileges)
        _execute_as_relation_owner(
            bind,
            relation,
            f"GRANT {privilege_sql} ON TABLE {relation} TO {_AUTH_ROLE}",
        )


def _revoke_contract(bind) -> None:
    for relation in _TABLE_PRIVILEGES:
        _execute_as_relation_owner(
            bind,
            relation,
            f"REVOKE ALL PRIVILEGES ON TABLE {relation} FROM {_AUTH_ROLE}",
        )


def _verify_final_contract(bind) -> None:
    allowed_relations = set(_TABLE_PRIVILEGES)

    direct_rows = bind.execute(
        sa.text(
            """
            SELECT
                table_schema || '.' || table_name AS relation_name,
                privilege_type
            FROM information_schema.table_privileges
            WHERE grantee = :role
            ORDER BY table_schema, table_name, privilege_type
            """
        ),
        {"role": _AUTH_ROLE},
    ).all()

    actual: dict[str, set[str]] = {}
    for relation, privilege in direct_rows:
        actual.setdefault(str(relation), set()).add(str(privilege))

    expected = {
        relation: set(privileges)
        for relation, privileges in _TABLE_PRIVILEGES.items()
    }
    if actual != expected:
        raise RuntimeError(
            f"auth_runtime direct table ACL drift: expected={expected!r}, observed={actual!r}"
        )

    unexpected_relations = set(actual) - allowed_relations
    if unexpected_relations:
        raise RuntimeError(
            f"auth_runtime has unrelated direct table ACLs: {sorted(unexpected_relations)!r}"
        )

    for relation, expected_privileges in _TABLE_PRIVILEGES.items():
        for privilege in expected_privileges:
            if not _scalar(
                bind,
                "SELECT pg_catalog.has_table_privilege(CAST(:role AS name), :relation, :privilege)",
                {
                    "role": _AUTH_ROLE,
                    "relation": relation,
                    "privilege": privilege,
                },
            ):
                raise RuntimeError(
                    f"auth_runtime lacks required {privilege} on {relation}"
                )
        for privilege in _FORBIDDEN_TABLE_PRIVILEGES:
            if _scalar(
                bind,
                "SELECT pg_catalog.has_table_privilege(CAST(:role AS name), :relation, :privilege)",
                {
                    "role": _AUTH_ROLE,
                    "relation": relation,
                    "privilege": privilege,
                },
            ):
                raise RuntimeError(
                    f"auth_runtime has forbidden {privilege} on {relation}"
                )

    if _scalar(
        bind,
        "SELECT pg_catalog.has_schema_privilege(CAST(:role AS name), 'public', 'CREATE')",
        {"role": _AUTH_ROLE},
    ):
        raise RuntimeError("auth_runtime must not have CREATE on public schema")


def upgrade() -> None:
    bind = op.get_bind()
    _require_role_contract(bind)
    _grant_contract(bind)
    _verify_final_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_role_contract(bind)
    _revoke_contract(bind)

    remaining = _scalar(
        bind,
        "SELECT count(*)::bigint FROM information_schema.table_privileges WHERE grantee = :role",
        {"role": _AUTH_ROLE},
    )
    if remaining != 0:
        raise RuntimeError(
            "auth_runtime direct table ACLs remain after downgrade; refusing partial rollback"
        )
