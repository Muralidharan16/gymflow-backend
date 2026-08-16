"""P3E: revalidate live owner authority for fenced asset workers.

Revision ID: s07d8e9f0a33
Revises: r07d8e9f0a32
Create Date: 2026-08-16

The durable asset job stores immutable owner identity, but a surviving job must
also prove that the requester is still an authoritative owner when a worker
claims or finalizes it. Owner login already treats ``owners.email_verified`` as
a live authorization prerequisite. Bind the asynchronous claim/finalize
capabilities to that same mutable authority signal without changing job ACLs,
RLS, ownership, role membership, or any worker table privileges.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "s07d8e9f0a33"
down_revision = "r07d8e9f0a32"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"

_FUNCTIONS = {
    "claim_organization_asset_job": (
        "p_job_id uuid, p_lease_token uuid, p_lease_seconds integer",
        True,
    ),
    "finalize_organization_asset_job": (
        "p_job_id uuid, p_lease_token uuid, p_width integer, p_height integer, "
        "p_size_bytes bigint, p_content_type text",
        True,
    ),
}

_OWNER_BINDING = "AND owner_row.org_id = v_job.organization_id"
_LIVE_BINDING = _OWNER_BINDING + "\n                  AND owner_row.email_verified IS TRUE"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E live-owner authority migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(session_user, :role, 'SET')"),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _column_select(bind) -> bool:
    return bool(bind.execute(sa.text("""
        SELECT pg_catalog.has_column_privilege(
            :role, 'public.owners', 'email_verified', 'SELECT'
        )
    """), {"role": _SECURITY_OWNER}).scalar_one())


def _function_contract(bind, name: str, identity_args: str) -> dict[str, object]:
    row = bind.execute(sa.text("""
        SELECT procedure.oid,
               owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   'worker_runtime', procedure.oid, 'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   'app_runtime', procedure.oid, 'EXECUTE'
               ) AS api_execute,
               pg_catalog.has_function_privilege(
                   'auth_runtime', procedure.oid, 'EXECUTE'
               ) AS auth_execute,
               pg_catalog.has_function_privilege(
                   'lifecycle_maintenance_runtime', procedure.oid, 'EXECUTE'
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
               ) AS public_execute,
               pg_catalog.pg_get_functiondef(procedure.oid) AS definition
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = :name
          AND pg_catalog.pg_get_function_identity_arguments(procedure.oid) = :args
    """), {"name": name, "args": identity_args}).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"P3E asset capability is missing: {name}")
    if (
        row["owner_name"] != _SECURITY_OWNER
        or not bool(row["prosecdef"])
        or row["volatility"] != "v"
        or set(row["proconfig"] or [])
        != {"search_path=pg_catalog", "row_security=on"}
        or not bool(row["worker_execute"])
        or bool(row["api_execute"])
        or bool(row["auth_execute"])
        or bool(row["maintenance_execute"])
        or bool(row["public_execute"])
    ):
        raise RuntimeError(f"P3E asset capability ACL/owner drift: {name}")
    return dict(row)


def _replace_live_binding(bind, *, enabled: bool) -> None:
    definitions: list[str] = []
    for name, (identity_args, _worker_only) in _FUNCTIONS.items():
        row = _function_contract(bind, name, identity_args)
        definition = str(row["definition"])
        if enabled:
            if _LIVE_BINDING in definition:
                raise RuntimeError(f"live-owner predicate already installed: {name}")
            if definition.count(_OWNER_BINDING) != 1:
                raise RuntimeError(f"asset owner predecessor predicate drift: {name}")
            patched = definition.replace(_OWNER_BINDING, _LIVE_BINDING, 1)
        else:
            if definition.count(_LIVE_BINDING) != 1:
                raise RuntimeError(f"live-owner predicate drift: {name}")
            patched = definition.replace(_LIVE_BINDING, _OWNER_BINDING, 1)
        definitions.append(patched)

    bind.exec_driver_sql("SET LOCAL ROLE app_security_owner")
    try:
        for definition in definitions:
            bind.exec_driver_sql(definition)
    finally:
        bind.exec_driver_sql("RESET ROLE")

    for name, (identity_args, _worker_only) in _FUNCTIONS.items():
        definition = str(_function_contract(bind, name, identity_args)["definition"])
        if enabled and definition.count(_LIVE_BINDING) != 1:
            raise RuntimeError(f"live-owner predicate was not installed: {name}")
        if not enabled and _LIVE_BINDING in definition:
            raise RuntimeError(f"live-owner predicate survived downgrade: {name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    if _column_select(bind):
        raise RuntimeError(
            "P3E predecessor unexpectedly grants app_security_owner owners.email_verified"
        )

    op.execute(
        "GRANT SELECT (email_verified) ON TABLE public.owners TO app_security_owner"
    )
    if not _column_select(bind):
        raise RuntimeError("P3E live-owner read grant was not installed")

    _replace_live_binding(bind, enabled=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    if not _column_select(bind):
        raise RuntimeError("P3E live-owner read grant drifted before downgrade")

    _replace_live_binding(bind, enabled=False)
    op.execute(
        "REVOKE SELECT (email_verified) ON TABLE public.owners FROM app_security_owner"
    )
    if _column_select(bind):
        raise RuntimeError("P3E live-owner read grant survived downgrade")
