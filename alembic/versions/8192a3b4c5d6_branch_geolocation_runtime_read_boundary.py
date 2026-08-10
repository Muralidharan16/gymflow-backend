"""Grant the ordinary runtime read-only access to branch geolocation state.

Revision ID: 8192a3b4c5d6
Revises: 708192a3b4c5
Create Date: 2026-08-10

``branch_geolocation_state`` is the tenant-scoped WKT projection backing the
latitude/longitude fields exposed by ordinary organization-address reads.  The
00f migration already ENABLEs and FORCEs RLS and owns the tenant policy, but the
reduced runtime ACL boundary omitted object-level SELECT.  That omission makes
legitimate branch/address reads fail before RLS can evaluate the tenant.

This revision owns only that missing read capability.  It grants no write,
destructive, schema-create, ownership, or RLS-bypass capability and restores the
exact predecessor ACL on downgrade.
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "8192a3b4c5d6"
down_revision = "708192a3b4c5"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_RUNTIME_ROLE = "app_runtime"
_RELATION = "branch_geolocation_state"
_POLICY = "geolocation_state_tenant_isolation"
_TENANT_EXPR = "org_id=nullifcurrent_setting'app.current_org_id'::text,true,''::text::uuid"
_FORBIDDEN = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _direct_privileges(bind, role_name: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :relation_name
                  AND grantee.rolname = :role_name
                """
            ),
            {"relation_name": _RELATION, "role_name": role_name},
        ).scalars().all()
    )


def _require_identity_and_runtime_role(bind) -> None:
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

    if (
        identity["session_name"] != _MIGRATION_OWNER
        or identity["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("geolocation runtime migration requires migration_owner")
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

    runtime = bind.execute(
        sa.text(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolinherit,
                rolcreatedb,
                rolcreaterole,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :runtime_role
            """
        ),
        {"runtime_role": _RUNTIME_ROLE},
    ).mappings().one_or_none()
    if runtime is None:
        raise RuntimeError("required app_runtime role is missing")
    if any(bool(runtime[key]) for key in runtime):
        raise RuntimeError(
            "app_runtime violates NOLOGIN/NOINHERIT/NOBYPASSRLS reduced-role contract"
        )


def _normalized_tenant_expr(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s()]", "", str(value).lower())


def _require_relation_and_policy(bind) -> None:
    relation = bind.execute(
        sa.text(
            """
            SELECT
                owner_role.rolname::text AS owner_name,
                relation_data.relrowsecurity,
                relation_data.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = :relation_name
              AND relation_data.relkind IN ('r', 'p')
            """
        ),
        {"relation_name": _RELATION},
    ).mappings().one_or_none()
    if relation is None:
        raise RuntimeError(f"required public.{_RELATION} relation is missing")
    if relation["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            f"unexpected owner for public.{_RELATION}: {relation['owner_name']!r}"
        )
    if not relation["relrowsecurity"] or not relation["relforcerowsecurity"]:
        raise RuntimeError(
            f"public.{_RELATION} must retain ENABLE + FORCE ROW LEVEL SECURITY"
        )

    policies = bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polname::text AS policy_name,
                policy_data.polcmd::text AS command,
                pg_catalog.pg_get_expr(
                    policy_data.polqual, policy_data.polrelid, true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck, policy_data.polrelid, true
                )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = policy_data.polrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = :relation_name
            """
        ),
        {"relation_name": _RELATION},
    ).mappings().all()
    if len(policies) != 1 or policies[0]["policy_name"] != _POLICY:
        raise RuntimeError(
            f"public.{_RELATION} tenant-policy inventory drifted: "
            f"{[row['policy_name'] for row in policies]!r}"
        )
    policy = policies[0]
    if (
        policy["command"] != "*"
        or _normalized_tenant_expr(policy["using_expr"]) != _TENANT_EXPR
        or _normalized_tenant_expr(policy["check_expr"]) != _TENANT_EXPR
    ):
        raise RuntimeError(f"public.{_RELATION} tenant policy drifted")


def _require_no_public_table_privileges(bind) -> None:
    observed = set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :relation_name
                  AND acl.grantee = 0
                """
            ),
            {"relation_name": _RELATION},
        ).scalars().all()
    )
    leaked = observed & ({"SELECT"} | _FORBIDDEN)
    if leaked:
        raise RuntimeError(
            f"PUBLIC unexpectedly has {sorted(leaked)!r} on public.{_RELATION}"
        )


def _require_predecessor(bind) -> None:
    _require_relation_and_policy(bind)
    _require_no_public_table_privileges(bind)
    observed = _direct_privileges(bind, _RUNTIME_ROLE)
    if observed:
        raise RuntimeError(
            f"geolocation runtime predecessor ACL drift: {sorted(observed)!r}"
        )


def _require_forward(bind) -> None:
    _require_relation_and_policy(bind)
    _require_no_public_table_privileges(bind)

    observed = _direct_privileges(bind, _RUNTIME_ROLE)
    if observed != {"SELECT"}:
        raise RuntimeError(
            "app_runtime geolocation direct ACL drift: "
            f"expected=['SELECT'], observed={sorted(observed)!r}"
        )
    if not _scalar(
        bind,
        """
        SELECT pg_catalog.has_table_privilege(
            CAST(:role_name AS name), :relation_name, 'SELECT'
        )
        """,
        {"role_name": _RUNTIME_ROLE, "relation_name": f"public.{_RELATION}"},
    ):
        raise RuntimeError(f"app_runtime lacks SELECT on public.{_RELATION}")

    for privilege in _FORBIDDEN:
        if _scalar(
            bind,
            """
            SELECT pg_catalog.has_table_privilege(
                CAST(:role_name AS name), :relation_name, :privilege
            )
            """,
            {
                "role_name": _RUNTIME_ROLE,
                "relation_name": f"public.{_RELATION}",
                "privilege": privilege,
            },
        ):
            raise RuntimeError(
                f"app_runtime must not have {privilege} on public.{_RELATION}"
            )

    if _scalar(
        bind,
        """
        SELECT pg_catalog.has_schema_privilege(
            CAST(:role_name AS name), 'public', 'CREATE'
        )
        """,
        {"role_name": _RUNTIME_ROLE},
    ):
        raise RuntimeError("app_runtime must not have CREATE on public schema")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_runtime_role(bind)
    _require_predecessor(bind)

    op.execute(
        "GRANT SELECT ON TABLE public.branch_geolocation_state TO app_runtime"
    )

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_runtime_role(bind)
    _require_forward(bind)

    op.execute(
        "REVOKE SELECT ON TABLE public.branch_geolocation_state FROM app_runtime"
    )

    _require_predecessor(bind)
