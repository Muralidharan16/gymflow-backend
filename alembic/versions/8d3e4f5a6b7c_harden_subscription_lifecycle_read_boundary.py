"""Harden subscription lifecycle read access for the reduced API runtime.

Revision ID: 8d3e4f5a6b7c
Revises: 7c2f91e4ab63
Create Date: 2026-08-13

The subscription lifecycle repository is a read model used by ordinary API
requests, but the lifecycle foundation predates the reduced ``app_runtime``
identity and never established a database read boundary. Granting SELECT
without row security would make a missed application predicate a cross-tenant
leak, so this revision establishes one narrow read-only tenant contract:

* app_runtime receives SELECT only on the six lifecycle read tables;
* every exposed table uses ENABLE + FORCE RLS;
* tenant visibility is bound to transaction-local ``app.current_org_id``;
* no write/destructive privilege, role mutation, ownership change, or
  BYPASSRLS capability is introduced.

Downgrade restores the exact predecessor posture: no app_runtime lifecycle ACL,
no lifecycle tenant-select policy, and RLS disabled on these tables.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8d3e4f5a6b7c"
down_revision = "7c2f91e4ab63"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_API = "app_runtime"
_TABLES = (
    "subscription_series",
    "subscription_terms",
    "subscription_term_slots",
    "subscription_slot_assignments",
    "subscription_freezes",
    "subscription_events",
)
_TENANT_EXPR = (
    "org_id = NULLIF(current_setting('app.current_org_id'::text, true), '')::uuid"
)


def _bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("8d3e lifecycle runtime boundary requires online catalog access")
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable")
    return bind


def _require_reduced_identities(bind) -> None:
    migration = bind.execute(
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
    if migration["session_name"] != _MIGRATION_OWNER or migration["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("8d3e requires session_user=current_user=migration_owner")
    if any(
        bool(migration[key])
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

    api = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _API},
    ).mappings().one_or_none()
    if api is None or any(bool(value) for value in api.values()):
        raise RuntimeError("app_runtime must remain a reduced NOLOGIN/NOBYPASSRLS capability role")

    for capability in (_API, "auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime"):
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"),
            {"member": _MIGRATION_OWNER, "role": capability},
        ).scalar_one():
            raise RuntimeError(f"migration_owner must not inherit runtime capability {capability}")
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'SET')"),
            {"member": _MIGRATION_OWNER, "role": capability},
        ).scalar_one():
            raise RuntimeError(f"migration_owner must not SET ROLE to runtime capability {capability}")


def _role_oid(bind, role_name: str) -> int:
    oid = bind.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role_name"),
        {"role_name": role_name},
    ).scalar_one_or_none()
    if oid is None:
        raise RuntimeError(f"8d3e required role is absent: {role_name}")
    return int(oid)


def _relation_state(bind, table_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text AS owner_name,
                   relation.relrowsecurity,
                   relation.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation.relowner
            WHERE namespace.nspname = 'public'
              AND relation.relname = :table_name
              AND relation.relkind = 'r'
            """
        ),
        {"table_name": table_name},
    ).mappings().one_or_none()


def _direct_table_privileges(bind, table_name: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
                ) AS acl
                JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = :table_name
                  AND grantee.rolname = :role_name
                """
            ),
            {"table_name": table_name, "role_name": _API},
        ).scalars().all()
    )


def _policy_name(table_name: str) -> str:
    return f"p_{table_name}_tenant_select"


def _policy_rows(bind, table_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT policy.polname::text AS name,
                   policy.polcmd::text AS command,
                   policy.polpermissive AS permissive,
                   policy.polroles AS roles,
                   pg_catalog.pg_get_expr(policy.polqual, policy.polrelid, true)::text AS using_expr,
                   pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid, true)::text AS check_expr
            FROM pg_catalog.pg_policy AS policy
            JOIN pg_catalog.pg_class AS relation ON relation.oid = policy.polrelid
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = :table_name
            ORDER BY policy.polname
            """
        ),
        {"table_name": table_name},
    ).mappings().all()


def _normalize_expression(value: object) -> str:
    return " ".join(str(value or "").split()).lower()


def _require_predecessor(bind) -> None:
    for table_name in _TABLES:
        state = _relation_state(bind, table_name)
        if state is None:
            raise RuntimeError(f"8d3e required relation is absent: public.{table_name}")
        if state["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"8d3e predecessor owner drift on public.{table_name}: {state['owner_name']!r}"
            )
        if bool(state["relrowsecurity"]) or bool(state["relforcerowsecurity"]):
            raise RuntimeError(f"8d3e predecessor unexpectedly already has RLS on public.{table_name}")
        acl = _direct_table_privileges(bind, table_name)
        if acl:
            raise RuntimeError(
                f"8d3e predecessor app_runtime ACL drift on public.{table_name}: {sorted(acl)!r}"
            )
        policies = _policy_rows(bind, table_name)
        if policies:
            raise RuntimeError(
                f"8d3e predecessor unexpectedly has policies on public.{table_name}: "
                f"{[row['name'] for row in policies]!r}"
            )


def _require_forward(bind) -> None:
    api_oid = _role_oid(bind, _API)
    for table_name in _TABLES:
        state = _relation_state(bind, table_name)
        if state is None or state["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"8d3e forward owner/relation drift on public.{table_name}")
        if not bool(state["relrowsecurity"]) or not bool(state["relforcerowsecurity"]):
            raise RuntimeError(f"8d3e must ENABLE and FORCE RLS on public.{table_name}")
        acl = _direct_table_privileges(bind, table_name)
        if acl != {"SELECT"}:
            raise RuntimeError(
                f"8d3e app_runtime ACL must be SELECT-only on public.{table_name}: {sorted(acl)!r}"
            )
        for forbidden in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            if bind.execute(
                sa.text("SELECT pg_catalog.has_table_privilege(:role, :relation, :privilege)"),
                {
                    "role": _API,
                    "relation": f"public.{table_name}",
                    "privilege": forbidden,
                },
            ).scalar_one():
                raise RuntimeError(
                    f"8d3e app_runtime unexpectedly has {forbidden} on public.{table_name}"
                )

        policies = _policy_rows(bind, table_name)
        if len(policies) != 1:
            raise RuntimeError(
                f"8d3e requires exactly one lifecycle policy on public.{table_name}: "
                f"{[row['name'] for row in policies]!r}"
            )
        policy = policies[0]
        if policy["name"] != _policy_name(table_name):
            raise RuntimeError(f"8d3e unexpected policy name on public.{table_name}: {policy['name']!r}")
        if policy["command"] != "r" or not bool(policy["permissive"]):
            raise RuntimeError(f"8d3e lifecycle policy command/permissiveness drift on public.{table_name}")
        if list(policy["roles"]) != [api_oid]:
            raise RuntimeError(
                f"8d3e lifecycle policy must target only app_runtime on public.{table_name}: "
                f"{list(policy['roles'])!r}"
            )
        using_expr = _normalize_expression(policy["using_expr"])
        for token in ("org_id", "current_setting", "app.current_org_id"):
            if token not in using_expr:
                raise RuntimeError(
                    f"8d3e tenant policy expression drift on public.{table_name}: {policy['using_expr']!r}"
                )
        if policy["check_expr"] is not None:
            raise RuntimeError(f"8d3e SELECT policy must not carry WITH CHECK on public.{table_name}")


def _require_restored(bind) -> None:
    for table_name in _TABLES:
        state = _relation_state(bind, table_name)
        if state is None or state["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"8d3e downgrade owner/relation drift on public.{table_name}")
        if bool(state["relrowsecurity"]) or bool(state["relforcerowsecurity"]):
            raise RuntimeError(f"8d3e downgrade failed to restore RLS-off state on public.{table_name}")
        acl = _direct_table_privileges(bind, table_name)
        if acl:
            raise RuntimeError(
                f"8d3e downgrade left app_runtime ACL on public.{table_name}: {sorted(acl)!r}"
            )
        if _policy_rows(bind, table_name):
            raise RuntimeError(f"8d3e downgrade left policy on public.{table_name}")


def upgrade() -> None:
    bind = _bind()
    _require_reduced_identities(bind)
    _require_predecessor(bind)

    for table_name in _TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table_name} TO app_runtime")
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {_policy_name(table_name)} ON public.{table_name} "
            f"FOR SELECT TO app_runtime USING ({_TENANT_EXPR})"
        )

    _require_forward(bind)


def downgrade() -> None:
    bind = _bind()
    _require_reduced_identities(bind)
    _require_forward(bind)

    for table_name in reversed(_TABLES):
        op.execute(f"DROP POLICY {_policy_name(table_name)} ON public.{table_name}")
        op.execute(f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT ON TABLE public.{table_name} FROM app_runtime")

    _require_restored(bind)
