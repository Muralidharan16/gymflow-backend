"""Harden tenant runtime access for members, plans, and subscriptions.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-12

The ordinary API routes for member management, membership plans, and modern
member subscriptions execute through ``app_runtime``. Their tables predate the
current reduced-runtime role graph and never received an explicit runtime ACL /
FORCE-RLS contract. With the hardened CI identity this made legitimate API
writes impossible; granting DML without tenant RLS would instead reopen a much
larger production security hole.

This revision establishes one bounded tenant-domain contract:

* direct-org tables use ``app.current_org_id`` under ENABLE + FORCE RLS;
* member measurements derive tenant ownership through their parent member;
* no DELETE or TRUNCATE authority is granted;
* member and membership-plan UPDATE are column-scoped to fields written by the
  current API services;
* organization counters receive only the columns needed by the atomic UPSERT;
* modern subscriptions and slot rows are read/create-only for app_runtime.

No roles are created or altered, no ownership changes, no BYPASSRLS is used,
and downgrade restores the exact predecessor state: no app_runtime ACL, no
policies, and RLS disabled on these legacy business tables.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_API = "app_runtime"

_DIRECT_TENANT_TABLES = (
    "members",
    "membership_plans",
    "organization_counters",
    "member_subscriptions_v2",
    "subscription_members",
)
_MEASUREMENTS = "member_measurements"
_ALL_TABLES = _DIRECT_TENANT_TABLES + (_MEASUREMENTS,)

_MEMBER_UPDATE_COLUMNS = {
    "address",
    "blood_group",
    "date_of_birth",
    "email",
    "emergency_contact_name",
    "emergency_contact_phone",
    "gender",
    "home_branch_id",
    "is_active",
    "name",
    "notes",
    "phone",
    "status",
    "updated_at",
    "updated_by",
}

_PLAN_UPDATE_COLUMNS = {
    "archived_at",
    "description",
    "duration_unit",
    "duration_value",
    "max_members",
    "name",
    "price",
    "status",
    "updated_at",
    "valid_from",
    "valid_until",
}

_COUNTER_INSERT_COLUMNS = {"id", "org_id", "counter_key", "current_value"}
_COUNTER_SELECT_COLUMNS = {"current_value"}
_COUNTER_UPDATE_COLUMNS = {"current_value", "updated_at"}

_TENANT_EXPR = (
    "org_id = NULLIF(current_setting('app.current_org_id'::text, true), '')::uuid"
)
_MEASUREMENT_EXPR = (
    "EXISTS (SELECT 1 FROM public.members AS tenant_member "
    "WHERE tenant_member.id = member_measurements.member_id "
    "AND tenant_member.gym_id = member_measurements.gym_id "
    "AND tenant_member.org_id = "
    "NULLIF(current_setting('app.current_org_id'::text, true), '')::uuid)"
)

_POLICY_CONTRACT = {
    "members": ("SELECT", "INSERT", "UPDATE"),
    "membership_plans": ("SELECT", "INSERT", "UPDATE"),
    "organization_counters": ("SELECT", "INSERT", "UPDATE"),
    "member_subscriptions_v2": ("SELECT", "INSERT"),
    "subscription_members": ("SELECT", "INSERT"),
    "member_measurements": ("SELECT", "INSERT"),
}
_POLICY_COMMAND = {"SELECT": "r", "INSERT": "a", "UPDATE": "w"}


def _bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("c6d7 member runtime boundary requires online catalog access")
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable")
    return bind


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
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("c6d7 requires session_user=current_user=migration_owner")
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
        raise RuntimeError(f"c6d7 required role is absent: {role_name}")
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
              AND relation.relkind IN ('r', 'p')
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
                    COALESCE(
                        relation.relacl,
                        pg_catalog.acldefault('r', relation.relowner)
                    )
                ) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace.nspname = 'public'
                  AND relation.relname = :table_name
                  AND grantee.rolname = :role_name
                """
            ),
            {"table_name": table_name, "role_name": _API},
        ).scalars().all()
    )


def _column_privileges(bind, table_name: str, privilege: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                  AND grantee = :role_name
                  AND privilege_type = :privilege
                ORDER BY column_name
                """
            ),
            {
                "table_name": table_name,
                "role_name": _API,
                "privilege": privilege,
            },
        ).scalars().all()
    )


def _policy_rows(bind, table_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT policy.polname::text AS name,
                   policy.polcmd::text AS command,
                   policy.polpermissive AS permissive,
                   policy.polroles AS roles,
                   pg_catalog.pg_get_expr(
                       policy.polqual, policy.polrelid, true
                   )::text AS using_expr,
                   pg_catalog.pg_get_expr(
                       policy.polwithcheck, policy.polrelid, true
                   )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = policy.polrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = :table_name
            ORDER BY policy.polname
            """
        ),
        {"table_name": table_name},
    ).mappings().all()


def _policy_names(bind, table_name: str) -> set[str]:
    return {row["name"] for row in _policy_rows(bind, table_name)}


def _require_predecessor(bind) -> None:
    for table_name in _ALL_TABLES:
        state = _relation_state(bind, table_name)
        if state is None:
            raise RuntimeError(f"c6d7 required relation is absent: public.{table_name}")
        if state["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"c6d7 predecessor owner drift on public.{table_name}: "
                f"{state['owner_name']!r}"
            )
        if bool(state["relrowsecurity"]) or bool(state["relforcerowsecurity"]):
            raise RuntimeError(
                f"c6d7 predecessor unexpectedly already has RLS on public.{table_name}"
            )
        observed_acl = _direct_table_privileges(bind, table_name)
        if observed_acl:
            raise RuntimeError(
                f"c6d7 predecessor app_runtime table ACL drift on public.{table_name}: "
                f"{sorted(observed_acl)!r}"
            )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            observed_columns = _column_privileges(bind, table_name, privilege)
            if observed_columns:
                raise RuntimeError(
                    f"c6d7 predecessor app_runtime column ACL drift on public.{table_name} "
                    f"for {privilege}: {sorted(observed_columns)!r}"
                )
        policies = _policy_names(bind, table_name)
        if policies:
            raise RuntimeError(
                f"c6d7 predecessor unexpectedly has policies on public.{table_name}: "
                f"{sorted(policies)!r}"
            )


def _policy_name(table_name: str, command: str) -> str:
    return f"p_{table_name}_tenant_{command.lower()}"


def _create_policy(table_name: str, command: str, expression: str) -> None:
    name = _policy_name(table_name, command)
    if command == "SELECT":
        op.execute(
            f"CREATE POLICY {name} ON public.{table_name} "
            f"FOR SELECT TO app_runtime USING ({expression})"
        )
    elif command == "INSERT":
        op.execute(
            f"CREATE POLICY {name} ON public.{table_name} "
            f"FOR INSERT TO app_runtime WITH CHECK ({expression})"
        )
    elif command == "UPDATE":
        op.execute(
            f"CREATE POLICY {name} ON public.{table_name} "
            f"FOR UPDATE TO app_runtime USING ({expression}) WITH CHECK ({expression})"
        )
    else:  # pragma: no cover - migration-local programming error
        raise RuntimeError(f"unsupported c6d7 policy command: {command}")


def _grant_forward_acl() -> None:
    op.execute("GRANT SELECT, INSERT ON TABLE public.members TO app_runtime")
    op.execute(
        "GRANT UPDATE (" + ", ".join(sorted(_MEMBER_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.members TO app_runtime"
    )

    op.execute("GRANT SELECT, INSERT ON TABLE public.membership_plans TO app_runtime")
    op.execute(
        "GRANT UPDATE (" + ", ".join(sorted(_PLAN_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.membership_plans TO app_runtime"
    )

    op.execute(
        "GRANT INSERT (" + ", ".join(sorted(_COUNTER_INSERT_COLUMNS)) + ") "
        "ON TABLE public.organization_counters TO app_runtime"
    )
    op.execute(
        "GRANT SELECT (" + ", ".join(sorted(_COUNTER_SELECT_COLUMNS)) + ") "
        "ON TABLE public.organization_counters TO app_runtime"
    )
    op.execute(
        "GRANT UPDATE (" + ", ".join(sorted(_COUNTER_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.organization_counters TO app_runtime"
    )

    op.execute("GRANT SELECT, INSERT ON TABLE public.member_subscriptions_v2 TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON TABLE public.subscription_members TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON TABLE public.member_measurements TO app_runtime")


def _revoke_forward_acl() -> None:
    op.execute(
        "REVOKE UPDATE (" + ", ".join(sorted(_MEMBER_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.members FROM app_runtime"
    )
    op.execute("REVOKE SELECT, INSERT ON TABLE public.members FROM app_runtime")

    op.execute(
        "REVOKE UPDATE (" + ", ".join(sorted(_PLAN_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.membership_plans FROM app_runtime"
    )
    op.execute("REVOKE SELECT, INSERT ON TABLE public.membership_plans FROM app_runtime")

    op.execute(
        "REVOKE UPDATE (" + ", ".join(sorted(_COUNTER_UPDATE_COLUMNS)) + ") "
        "ON TABLE public.organization_counters FROM app_runtime"
    )
    op.execute(
        "REVOKE SELECT (" + ", ".join(sorted(_COUNTER_SELECT_COLUMNS)) + ") "
        "ON TABLE public.organization_counters FROM app_runtime"
    )
    op.execute(
        "REVOKE INSERT (" + ", ".join(sorted(_COUNTER_INSERT_COLUMNS)) + ") "
        "ON TABLE public.organization_counters FROM app_runtime"
    )

    for table_name in (
        "member_subscriptions_v2",
        "subscription_members",
        "member_measurements",
    ):
        op.execute(f"REVOKE SELECT, INSERT ON TABLE public.{table_name} FROM app_runtime")


def _normalize_expression(value: object) -> str:
    return " ".join(str(value or "").split()).lower()


def _require_policy_contract(bind, table_name: str) -> None:
    rows = {row["name"]: row for row in _policy_rows(bind, table_name)}
    expected_names = {
        _policy_name(table_name, command)
        for command in _POLICY_CONTRACT[table_name]
    }
    if set(rows) != expected_names:
        raise RuntimeError(
            f"c6d7 policy inventory drift on public.{table_name}: "
            f"{sorted(rows)!r}"
        )

    api_oid = _role_oid(bind, _API)
    for command in _POLICY_CONTRACT[table_name]:
        row = rows[_policy_name(table_name, command)]
        if row["command"] != _POLICY_COMMAND[command]:
            raise RuntimeError(
                f"c6d7 policy command drift on public.{table_name}/{command}"
            )
        if not bool(row["permissive"]) or list(row["roles"]) != [api_oid]:
            raise RuntimeError(
                f"c6d7 policy role/permissive drift on public.{table_name}/{command}"
            )

        using_expr = _normalize_expression(row["using_expr"])
        check_expr = _normalize_expression(row["check_expr"])
        expected_tokens = ["app.current_org_id"]
        if table_name == _MEASUREMENTS:
            expected_tokens.extend(["members", "member_id", "gym_id"])

        if command == "SELECT":
            if check_expr or any(token not in using_expr for token in expected_tokens):
                raise RuntimeError(
                    f"c6d7 SELECT policy expression drift on public.{table_name}"
                )
        elif command == "INSERT":
            if using_expr or any(token not in check_expr for token in expected_tokens):
                raise RuntimeError(
                    f"c6d7 INSERT policy expression drift on public.{table_name}"
                )
        elif command == "UPDATE":
            if any(token not in using_expr for token in expected_tokens) or any(
                token not in check_expr for token in expected_tokens
            ):
                raise RuntimeError(
                    f"c6d7 UPDATE policy expression drift on public.{table_name}"
                )


def _require_forward(bind) -> None:
    expected_table_acl = {
        "members": {"SELECT", "INSERT"},
        "membership_plans": {"SELECT", "INSERT"},
        "organization_counters": set(),
        "member_subscriptions_v2": {"SELECT", "INSERT"},
        "subscription_members": {"SELECT", "INSERT"},
        "member_measurements": {"SELECT", "INSERT"},
    }
    expected_update = {
        "members": _MEMBER_UPDATE_COLUMNS,
        "membership_plans": _PLAN_UPDATE_COLUMNS,
        "organization_counters": _COUNTER_UPDATE_COLUMNS,
        "member_subscriptions_v2": set(),
        "subscription_members": set(),
        "member_measurements": set(),
    }

    for table_name in _ALL_TABLES:
        state = _relation_state(bind, table_name)
        if state is None or (bool(state["relrowsecurity"]), bool(state["relforcerowsecurity"])) != (True, True):
            raise RuntimeError(f"c6d7 forward FORCE-RLS drift on public.{table_name}")

        table_acl = _direct_table_privileges(bind, table_name)
        if table_acl != expected_table_acl[table_name]:
            raise RuntimeError(
                f"c6d7 forward table ACL drift on public.{table_name}: "
                f"{sorted(table_acl)!r}"
            )
        update_columns = _column_privileges(bind, table_name, "UPDATE")
        if update_columns != expected_update[table_name]:
            raise RuntimeError(
                f"c6d7 forward UPDATE column drift on public.{table_name}: "
                f"{sorted(update_columns)!r}"
            )

        if table_name == "organization_counters":
            if _column_privileges(bind, table_name, "INSERT") != _COUNTER_INSERT_COLUMNS:
                raise RuntimeError("c6d7 organization_counters INSERT column drift")
            if _column_privileges(bind, table_name, "SELECT") != _COUNTER_SELECT_COLUMNS:
                raise RuntimeError("c6d7 organization_counters SELECT column drift")

        forbidden = table_acl & {"UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
        if forbidden:
            raise RuntimeError(
                f"c6d7 forbidden broad privilege on public.{table_name}: {sorted(forbidden)!r}"
            )
        if _column_privileges(bind, table_name, "DELETE"):
            raise RuntimeError(f"c6d7 unexpected DELETE column ACL on public.{table_name}")

        _require_policy_contract(bind, table_name)


def upgrade() -> None:
    bind = _bind()
    _require_migration_owner(bind)
    _require_predecessor(bind)

    for table_name in _ALL_TABLES:
        op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")

    _grant_forward_acl()

    for table_name in _DIRECT_TENANT_TABLES:
        for command in _POLICY_CONTRACT[table_name]:
            _create_policy(table_name, command, _TENANT_EXPR)
    for command in _POLICY_CONTRACT[_MEASUREMENTS]:
        _create_policy(_MEASUREMENTS, command, _MEASUREMENT_EXPR)

    _require_forward(bind)


def downgrade() -> None:
    bind = _bind()
    _require_migration_owner(bind)
    _require_forward(bind)

    for table_name in reversed(_ALL_TABLES):
        for command in reversed(_POLICY_CONTRACT[table_name]):
            op.execute(
                f"DROP POLICY {_policy_name(table_name, command)} ON public.{table_name}"
            )

    _revoke_forward_acl()

    for table_name in reversed(_ALL_TABLES):
        op.execute(f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table_name} DISABLE ROW LEVEL SECURITY")

    _require_predecessor(bind)
