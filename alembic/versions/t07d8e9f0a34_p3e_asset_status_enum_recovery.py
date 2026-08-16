"""P3E: preserve asset status enum during live-authority recovery.

Revision ID: t07d8e9f0a34
Revises: s07d8e9f0a33
Create Date: 2026-08-16

The s07 live-owner cancellation path restores a visible logo/cover status after
revocation. PostgreSQL correctly rejects an untyped CASE expression when it is
assigned to ``asset_status_enum``. Replace only those recovery literals with an
explicit schema-qualified enum cast. No ACL, RLS, ownership, role, lease, or
queue semantics change.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "t07d8e9f0a34"
down_revision = "s07d8e9f0a33"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"

_FUNCTIONS = {
    "claim_organization_asset_job": (
        "p_job_id uuid, p_lease_token uuid, p_lease_seconds integer"
    ),
    "finalize_organization_asset_job": (
        "p_job_id uuid, p_lease_token uuid, p_width integer, p_height integer, "
        "p_size_bytes bigint, p_content_type text"
    ),
}

_LOGO_UNTYPED = "WHEN organization.logo_key IS NULL THEN NULL ELSE 'ready'"
_LOGO_TYPED = (
    "WHEN organization.logo_key IS NULL THEN NULL "
    "ELSE 'ready'::public.asset_status_enum"
)
_COVER_UNTYPED = "WHEN organization.cover_key IS NULL THEN NULL ELSE 'ready'"
_COVER_TYPED = (
    "WHEN organization.cover_key IS NULL THEN NULL "
    "ELSE 'ready'::public.asset_status_enum"
)


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E enum recovery migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(session_user, :role, 'SET')"),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _definition(bind, name: str, identity_args: str) -> str:
    row = bind.execute(sa.text("""
        SELECT pg_catalog.pg_get_functiondef(procedure.oid) AS definition,
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
               ) AS public_execute
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
        raise RuntimeError(f"P3E asset capability contract drift: {name}")
    return str(row["definition"])


def _replace(bind, *, typed: bool) -> None:
    definitions: list[str] = []
    for name, identity_args in _FUNCTIONS.items():
        definition = _definition(bind, name, identity_args)
        if typed:
            if definition.count(_LOGO_UNTYPED) != 1 or definition.count(_COVER_UNTYPED) != 1:
                raise RuntimeError(f"P3E untyped recovery predicate drift: {name}")
            definition = definition.replace(_LOGO_UNTYPED, _LOGO_TYPED, 1)
            definition = definition.replace(_COVER_UNTYPED, _COVER_TYPED, 1)
        else:
            if definition.count(_LOGO_TYPED) != 1 or definition.count(_COVER_TYPED) != 1:
                raise RuntimeError(f"P3E typed recovery predicate drift: {name}")
            definition = definition.replace(_LOGO_TYPED, _LOGO_UNTYPED, 1)
            definition = definition.replace(_COVER_TYPED, _COVER_UNTYPED, 1)
        definitions.append(definition)

    bind.exec_driver_sql("SET LOCAL ROLE app_security_owner")
    try:
        for definition in definitions:
            bind.exec_driver_sql(definition)
    finally:
        bind.exec_driver_sql("RESET ROLE")

    for name, identity_args in _FUNCTIONS.items():
        definition = _definition(bind, name, identity_args)
        if typed:
            if definition.count(_LOGO_TYPED) != 1 or definition.count(_COVER_TYPED) != 1:
                raise RuntimeError(f"P3E typed recovery was not installed: {name}")
        elif _LOGO_TYPED in definition or _COVER_TYPED in definition:
            raise RuntimeError(f"P3E typed recovery survived downgrade: {name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _replace(bind, typed=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _replace(bind, typed=False)
