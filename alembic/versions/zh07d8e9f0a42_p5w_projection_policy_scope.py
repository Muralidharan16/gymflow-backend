"""Scope the legacy projection policy away from the worker runtime.

Revision ID: zh07d8e9f0a42
Revises: zg07d8e9f0a41
Create Date: 2026-09-12

``tenant_isolation_projection`` was created without a ``TO`` clause and
therefore remained applicable to PUBLIC after the dedicated branch-hours worker
policies were added. PostgreSQL could consequently evaluate its
``organization_members`` predicate during a worker projection upsert even when
the lease-bound worker policy authorized the row. The worker intentionally has
no access to that PII-bearing relation, so the durable operation failed closed.

This revision changes only the legacy policy audience from PUBLIC to
``app_runtime``. It preserves the exact command, permissive mode and predicate,
keeps the dedicated worker policies lease-bound, and introduces no grant or
BYPASSRLS capability. Downgrade restores the exact predecessor audience.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zh07d8e9f0a42"
down_revision = "zg07d8e9f0a41"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RELATION = "public.branch_hours_projection"
_POLICY = "tenant_isolation_projection"
_APPLICATION_ROLE = "app_runtime"
_WORKER_ROLE = "worker_runtime"
_WORKER_POLICIES = {
    "branch_hours_worker_projection_read": "r",
    "branch_hours_worker_projection_insert": "a",
    "branch_hours_worker_projection_update": "w",
}


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("zh07 P5-W2 projection policy repair requires migration_owner")
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
        raise RuntimeError("zh07 migration_owner violates the reduced-role contract")


def _role_oid(bind, role_name: str) -> int:
    value = bind.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:role_name"),
        {"role_name": role_name},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"zh07 required role is absent: {role_name}")
    return int(value)


def _policy_row(bind, policy_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT policy_data.polcmd::text AS command,
                   policy_data.polpermissive,
                   policy_data.polroles,
                   pg_catalog.pg_get_expr(
                       policy_data.polqual,policy_data.polrelid,true
                   )::text AS using_expr,
                   pg_catalog.pg_get_expr(
                       policy_data.polwithcheck,policy_data.polrelid,true
                   )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid=CAST(:relation AS regclass)
              AND policy_data.polname=:policy_name
            """
        ),
        {"relation": _RELATION, "policy_name": policy_name},
    ).mappings().one_or_none()


def _predicate_contract(row) -> tuple[object, ...]:
    return (
        row["command"],
        bool(row["polpermissive"]),
        row["using_expr"],
        row["check_expr"],
    )


def _relation_contract(bind) -> tuple[object, ...]:
    row = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text,
                   relation_data.relrowsecurity,
                   relation_data.relforcerowsecurity,
                   relation_data.relacl::text
            FROM pg_catalog.pg_class AS relation_data
            WHERE relation_data.oid=CAST(:relation AS regclass)
            """
        ),
        {"relation": _RELATION},
    ).one_or_none()
    if row is None:
        raise RuntimeError(f"zh07 predecessor relation is absent: {_RELATION}")
    contract = (str(row[0]), bool(row[1]), bool(row[2]), row[3])
    if contract[0] != _MIGRATION_OWNER or contract[1:3] != (True, True):
        raise RuntimeError(f"zh07 predecessor owner/FORCE-RLS drift: {contract!r}")
    return contract


def _has_table_privilege(bind, role_name: str, relation: str, privilege: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                ":role_name,:relation,:privilege)"
            ),
            {
                "role_name": role_name,
                "relation": relation,
                "privilege": privilege,
            },
        ).scalar_one()
    )


def _assert_worker_boundary(bind) -> None:
    if _has_table_privilege(
        bind, _WORKER_ROLE, "public.organization_members", "SELECT"
    ):
        raise RuntimeError("zh07 worker unexpectedly has organization_members SELECT")
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        if not _has_table_privilege(bind, _WORKER_ROLE, _RELATION, privilege):
            raise RuntimeError(
                f"zh07 worker projection privilege is absent: {privilege}"
            )
    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        if _has_table_privilege(bind, _WORKER_ROLE, _RELATION, privilege):
            raise RuntimeError(
                f"zh07 worker projection privilege widened: {privilege}"
            )
    if bind.execute(
        sa.text(
            "SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname=:role_name"
        ),
        {"role_name": _WORKER_ROLE},
    ).scalar_one():
        raise RuntimeError("zh07 worker role bypasses the RLS boundary")


def _capture_boundary(bind, expected_roles: list[int]) -> dict[str, object]:
    relation_contract = _relation_contract(bind)
    tenant_policy = _policy_row(bind, _POLICY)
    if tenant_policy is None:
        raise RuntimeError(f"zh07 predecessor policy is absent: {_POLICY}")
    if list(tenant_policy["polroles"]) != expected_roles:
        raise RuntimeError(
            "zh07 refuses projection policy audience drift: "
            f"observed={list(tenant_policy['polroles'])!r} expected={expected_roles!r}"
        )
    if (
        tenant_policy["command"] != "*"
        or not bool(tenant_policy["polpermissive"])
        or tenant_policy["using_expr"] is None
        or tenant_policy["check_expr"] is not None
        or "organization_members" not in str(tenant_policy["using_expr"])
    ):
        raise RuntimeError("zh07 predecessor projection policy contract drifted")

    worker_oid = _role_oid(bind, _WORKER_ROLE)
    worker_policies: dict[str, tuple[object, ...]] = {}
    for policy_name, command in _WORKER_POLICIES.items():
        row = _policy_row(bind, policy_name)
        if row is None:
            raise RuntimeError(f"zh07 worker policy is absent: {policy_name}")
        if (
            row["command"] != command
            or not bool(row["polpermissive"])
            or list(row["polroles"]) != [worker_oid]
        ):
            raise RuntimeError(f"zh07 worker policy contract drifted: {policy_name}")
        worker_policies[policy_name] = (
            *_predicate_contract(row),
            tuple(row["polroles"]),
        )

    _assert_worker_boundary(bind)
    return {
        "relation": relation_contract,
        "tenant_predicate": _predicate_contract(tenant_policy),
        "worker_policies": worker_policies,
    }


def _verify_boundary(
    bind,
    *,
    expected_roles: list[int],
    predecessor: dict[str, object],
) -> None:
    current = _capture_boundary(bind, expected_roles)
    if current != predecessor:
        raise RuntimeError("zh07 changed a contract other than the policy audience")


def _alter_policy_scope(role_name: str) -> None:
    op.execute(f"ALTER POLICY {_POLICY} ON {_RELATION} TO {role_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    predecessor = _capture_boundary(bind, [0])
    _alter_policy_scope(_APPLICATION_ROLE)
    _verify_boundary(
        bind,
        expected_roles=[_role_oid(bind, _APPLICATION_ROLE)],
        predecessor=predecessor,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    predecessor = _capture_boundary(bind, [_role_oid(bind, _APPLICATION_ROLE)])
    _alter_policy_scope("PUBLIC")
    _verify_boundary(bind, expected_roles=[0], predecessor=predecessor)
