"""Bind P4C notification recipient reads to the internal worker context.

Revision ID: zb07d8e9f0a3c
Revises: za07d8e9f0a3b
Create Date: 2026-08-17

P4C notification fanout and delivery claims resolve recipients from the live
``members`` projection after establishing a worker-owned outbox lease.  The
members table is FORCE RLS and its tenant CRUD policies intentionally target
``app_runtime`` only.  Column-scoped SELECT granted to ``app_security_owner``
is therefore insufficient for the SECURITY DEFINER notification functions.

This corrective revision adds a separate SELECT policy for
``app_security_owner``.  It is usable only in the exact lifecycle-worker session
context and only for members belonging to ``app.current_org_id``.  Runtime
roles receive no new table privilege and cannot SET ROLE to the security owner.
The notification functions continue to narrow rows to their persisted branch,
member and command authority after the live lease fence is established.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zb07d8e9f0a3c"
down_revision = "za07d8e9f0a3b"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MEMBERS = "public.members"
_POLICY = "p4c_notification_member_security_owner_select"
_MEMBER_COLUMNS = (
    "id",
    "org_id",
    "home_branch_id",
    "name",
    "email",
    "status",
    "is_active",
)
_RUNTIME_ROLES = (
    "app_runtime",
    "auth_runtime",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
)


def _require_reduced_role(bind, role_name: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=:role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()
    if row is None or any(bool(row[key]) for key in row):
        raise RuntimeError(f"zb07 reduced-role contract drift: {role_name}")


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,current_user::text AS current_name,
                   rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("zb07 P4C migration requires migration_owner")
    if any(bool(row[key]) for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
    )):
        raise RuntimeError("zb07 migration_owner violates reduced-role contract")

    _require_reduced_role(bind, _SECURITY_OWNER)
    for role_name in _RUNTIME_ROLES:
        _require_reduced_role(bind, role_name)
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": role_name, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"zb07 runtime may SET ROLE app_security_owner: {role_name}")


def _require_predecessor(bind) -> None:
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
        {"relation": _MEMBERS},
    ).scalar_one():
        raise RuntimeError("zb07 missing predecessor relation public.members")

    enabled, forced = bind.execute(
        sa.text(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE oid=CAST(:relation AS regclass)"
        ),
        {"relation": _MEMBERS},
    ).one()
    if not enabled or not forced:
        raise RuntimeError("zb07 requires members ENABLE+FORCE RLS")

    for column in _MEMBER_COLUMNS:
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,:column,'SELECT')"
            ),
            {"role": _SECURITY_OWNER, "relation": _MEMBERS, "column": column},
        ).scalar_one():
            raise RuntimeError(
                f"zb07 missing P4C predecessor app_security_owner member column SELECT: {column}"
            )

    if bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_policy "
            "WHERE polrelid=CAST(:relation AS regclass) AND polname=:policy)"
        ),
        {"relation": _MEMBERS, "policy": _POLICY},
    ).scalar_one():
        raise RuntimeError("zb07 notification member policy collision")


def _post_install_proof(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   pg_catalog.pg_get_expr(p.polqual,p.polrelid) AS qualifier,
                   ARRAY(
                       SELECT r.rolname::text
                       FROM pg_catalog.pg_roles r
                       WHERE r.oid=ANY(p.polroles)
                       ORDER BY r.rolname
                   ) AS roles
            FROM pg_catalog.pg_policy p
            WHERE p.polrelid='public.members'::regclass
              AND p.polname=:policy
            """
        ),
        {"policy": _POLICY},
    ).mappings().one_or_none()
    if row is None or row["command"] != "r" or list(row["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification member policy role/command drift")
    qualifier = row["qualifier"] or ""
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "org_id",
    ):
        if token not in qualifier:
            raise RuntimeError(f"zb07 notification member policy lost scope token: {token}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)

    op.execute(
        """
        CREATE POLICY p4c_notification_member_security_owner_select
        ON public.members
        FOR SELECT TO app_security_owner
        USING (
            NULLIF(pg_catalog.current_setting('app.current_role',true),'') = 'branch_lifecycle_worker'
            AND NULLIF(pg_catalog.current_setting('app.internal_maintenance',true),'') = 'branch_lifecycle_saga'
            AND pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.worker_id',true),''),'uuid'
            )
            AND CASE
                WHEN pg_catalog.pg_input_is_valid(
                    NULLIF(pg_catalog.current_setting('app.current_org_id',true),''),'uuid'
                )
                THEN org_id=CAST(
                    NULLIF(pg_catalog.current_setting('app.current_org_id',true),'') AS uuid
                )
                ELSE false
            END
        )
        """
    )
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_member_security_owner_select "
        "ON public.members"
    )
