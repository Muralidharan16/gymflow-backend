"""P3A: decouple auth bootstrap from ordinary API runtime authority.

Revision ID: c47d8e9f0a1e
Revises: c37d8e9f0a1d
Create Date: 2026-08-14

The dedicated authentication deployment login historically inherited
``app_runtime`` because onboarding used the ordinary API address path. P3A now
makes organization-profile mutation executable by ``app_runtime`` only, so that
legacy role composition would also give the auth process an unrelated tenant-root
profile capability.

This revision removes the last database-object reason for that composition. The
NOLOGIN ``auth_runtime`` group receives only SELECT+INSERT on
``organization_addresses`` for verified first-tenant onboarding. The relation is
already ENABLE+FORCE RLS and its INSERT/SELECT policies are bound to
``app.current_org_id``. Branch/bootstrap, owner, trial, gym and audit permissions
remain owned by their existing migrations. Deployment-login membership itself is
cluster configuration and is certified by the runtime binding manifest/P2D gate.

No UPDATE/DELETE/TRUNCATE/schema-create/ownership/BYPASSRLS is added. Downgrade
restores the exact c37 predecessor ACL state.
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "c47d8e9f0a1e"
down_revision = "c37d8e9f0a1d"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_AUTH_ROLE = "auth_runtime"
_RUNTIME_ROLE = "app_runtime"
_ADDRESS = "public.organization_addresses"
_ALLOWED_AUTH_ADDRESS = {"SELECT", "INSERT"}
_FORBIDDEN_AUTH_ADDRESS = {"UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
_PROFILE_READ = "app_secure.current_organization_profile()"
_PROFILE_UPDATE = "app_secure.update_current_organization_profile(jsonb)"
_TENANT_EXPR = "org_id=nullifcurrent_setting'app.current_org_id'::text,true,''::text::uuid"


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _direct_table_privileges(bind, role_name: str, relation: str) -> set[str]:
    schema_name, relation_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :relation_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
                "role_name": role_name,
            },
        ).scalars().all()
    )


def _direct_function_execute(bind, role_name: str, signature: str) -> bool:
    schema_and_name, arguments = signature[:-1].split("(", 1)
    schema_name, function_name = schema_and_name.split(".", 1)
    rows = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS function_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = function_data.pronamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(function_data.proacl) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND function_data.proname = :function_name
                  AND pg_catalog.pg_get_function_identity_arguments(function_data.oid) = :arguments
                  AND grantee.rolname = :role_name
                  AND acl.privilege_type = 'EXECUTE'
            )
            """
        ),
        {
            "schema_name": schema_name,
            "function_name": function_name,
            "arguments": arguments,
            "role_name": role_name,
        },
    ).scalar_one()
    return bool(rows)


def _normalized(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s()]", "", str(value).lower())


def _require_identity_and_roles(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
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
    if identity["session_name"] != _MIGRATION_OWNER or identity["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("P3A auth decoupling migration requires migration_owner")
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
        raise RuntimeError("migration_owner violates the reduced role contract")

    rows = bind.execute(
        sa.text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                   rolcreaterole, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:auth_role, :runtime_role)
            """
        ),
        {"auth_role": _AUTH_ROLE, "runtime_role": _RUNTIME_ROLE},
    ).mappings().all()
    by_name = {row["rolname"]: row for row in rows}
    if set(by_name) != {_AUTH_ROLE, _RUNTIME_ROLE}:
        raise RuntimeError("required P3A runtime roles are missing")
    for role_name, row in by_name.items():
        if any(
            bool(row[key])
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolinherit",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise RuntimeError(
                f"managed role {role_name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS"
            )


def _require_address_security(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT owner.rolname::text AS owner_name,
                   relation_data.relrowsecurity,
                   relation_data.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation_data.relowner
            WHERE relation_data.oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _ADDRESS},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("organization_addresses is missing")
    if row["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            f"unexpected organization_addresses owner: {row['owner_name']!r}"
        )
    if (bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])) != (True, True):
        raise RuntimeError("organization_addresses must retain ENABLE + FORCE RLS")

    policies = bind.execute(
        sa.text(
            """
            SELECT policy_data.polname::text AS policy_name,
                   policy_data.polcmd::text AS command,
                   pg_catalog.pg_get_expr(
                       policy_data.polqual, policy_data.polrelid, true
                   )::text AS using_expr,
                   pg_catalog.pg_get_expr(
                       policy_data.polwithcheck, policy_data.polrelid, true
                   )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname IN (
                  'tenant_isolation_addr_select',
                  'tenant_isolation_addr_insert'
              )
            ORDER BY policy_data.polname
            """
        ),
        {"relation": _ADDRESS},
    ).mappings().all()
    by_name = {row["policy_name"]: row for row in policies}
    if set(by_name) != {"tenant_isolation_addr_select", "tenant_isolation_addr_insert"}:
        raise RuntimeError("required organization-address tenant policies are missing")
    select_policy = by_name["tenant_isolation_addr_select"]
    insert_policy = by_name["tenant_isolation_addr_insert"]
    if select_policy["command"] != "r" or _normalized(select_policy["using_expr"]) != _TENANT_EXPR:
        raise RuntimeError("organization-address SELECT tenant policy drifted")
    if insert_policy["command"] != "a" or _normalized(insert_policy["check_expr"]) != _TENANT_EXPR:
        raise RuntimeError("organization-address INSERT tenant policy drifted")


def _require_profile_function_separation(bind) -> None:
    for signature in (_PROFILE_READ, _PROFILE_UPDATE):
        if _direct_function_execute(bind, _AUTH_ROLE, signature):
            raise RuntimeError(
                f"auth_runtime has direct ordinary profile EXECUTE: {signature}"
            )


def _require_predecessor(bind) -> None:
    _require_identity_and_roles(bind)
    _require_address_security(bind)
    _require_profile_function_separation(bind)
    observed = _direct_table_privileges(bind, _AUTH_ROLE, _ADDRESS)
    if observed:
        raise RuntimeError(
            "c37 predecessor auth_runtime address ACL drift: "
            f"expected empty, observed={sorted(observed)!r}"
        )


def _require_forward(bind) -> None:
    _require_identity_and_roles(bind)
    _require_address_security(bind)
    _require_profile_function_separation(bind)
    observed = _direct_table_privileges(bind, _AUTH_ROLE, _ADDRESS)
    if observed != _ALLOWED_AUTH_ADDRESS:
        raise RuntimeError(
            "P3A auth_runtime address ACL drift: "
            f"expected={sorted(_ALLOWED_AUTH_ADDRESS)!r}, observed={sorted(observed)!r}"
        )
    for privilege in _FORBIDDEN_AUTH_ADDRESS:
        if _scalar(
            bind,
            "SELECT pg_catalog.has_table_privilege(CAST(:role AS name), :relation, :privilege)",
            {"role": _AUTH_ROLE, "relation": _ADDRESS, "privilege": privilege},
        ):
            raise RuntimeError(
                f"auth_runtime has forbidden {privilege} on organization_addresses"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    bind.execute(
        sa.text(
            "GRANT SELECT, INSERT ON TABLE public.organization_addresses TO auth_runtime"
        )
    )
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_forward(bind)
    bind.execute(
        sa.text(
            "REVOKE SELECT, INSERT ON TABLE public.organization_addresses FROM auth_runtime"
        )
    )
    _require_predecessor(bind)
