"""Bind P4C notification fanout history reads to the internal worker context.

Revision ID: za07d8e9f0a3b
Revises: z07d8e9f0a3a
Create Date: 2026-08-17

P4C notification fanout already validates a live, worker-owned
``branch.member_notification`` lease before consulting lifecycle history.  The
history table is FORCE RLS, however, and its predecessor SELECT policy is scoped
to app_runtime.  This corrective revision adds a separate app_security_owner
SELECT policy for the SECURITY DEFINER fanout path without widening the tenant
runtime policy or granting any runtime role direct history access.

The policy is tenant-bound and can be used only with the exact lifecycle-worker
session context.  Row selection remains correlation-bound inside the fanout
function after the live lease is established.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "za07d8e9f0a3b"
down_revision = "z07d8e9f0a3a"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_HISTORY = "public.branch_status_history"
_BRANCH = "public.org_branches"
_POLICY = "p4c_notification_history_security_owner_select"
_HISTORY_COLUMNS = ("branch_id", "from_status", "to_status", "correlation_id", "changed_at")
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
        raise RuntimeError(f"za07 reduced-role contract drift: {role_name}")


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
        raise RuntimeError("za07 P4C migration requires migration_owner")
    if any(bool(row[key]) for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
    )):
        raise RuntimeError("za07 migration_owner violates reduced-role contract")

    _require_reduced_role(bind, _SECURITY_OWNER)
    for role_name in _RUNTIME_ROLES:
        _require_reduced_role(bind, role_name)
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": role_name, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"za07 runtime may SET ROLE app_security_owner: {role_name}")


def _require_predecessor(bind) -> None:
    for relation in (_HISTORY, _BRANCH):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"za07 missing predecessor relation {relation}")

    enabled, forced = bind.execute(
        sa.text(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE oid=CAST(:relation AS regclass)"
        ),
        {"relation": _HISTORY},
    ).one()
    if not enabled or not forced:
        raise RuntimeError("za07 requires branch_status_history ENABLE+FORCE RLS")

    for column in _HISTORY_COLUMNS:
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,:column,'SELECT')"
            ),
            {"role": _SECURITY_OWNER, "relation": _HISTORY, "column": column},
        ).scalar_one():
            raise RuntimeError(
                f"za07 missing P4C predecessor app_security_owner history column SELECT: {column}"
            )

    # P4B owns the bounded app_security_owner branch projection read used to
    # translate branch_id to tenant_id inside this policy.  Do not duplicate or
    # broaden that predecessor authority here.
    if not bind.execute(
        sa.text(
            """
            SELECT EXISTS(
                SELECT 1 FROM pg_catalog.pg_policy p
                WHERE p.polrelid='public.org_branches'::regclass
                  AND p.polname='p4b_search_internal_branch_read'
                  AND (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='app_security_owner')
                      = ANY(p.polroles)
            )
            """
        )
    ).scalar_one():
        raise RuntimeError("za07 requires certified P4B app_security_owner branch read policy")

    if bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_policy "
            "WHERE polrelid=CAST(:relation AS regclass) AND polname=:policy)"
        ),
        {"relation": _HISTORY, "policy": _POLICY},
    ).scalar_one():
        raise RuntimeError("za07 notification history policy collision")

    for role_name in _RUNTIME_ROLES:
        if bind.execute(
            sa.text("SELECT pg_catalog.has_table_privilege(:role,:relation,'SELECT')"),
            {"role": role_name, "relation": _HISTORY},
        ).scalar_one():
            raise RuntimeError(f"za07 refuses runtime history table SELECT expansion: {role_name}")


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
            WHERE p.polrelid='public.branch_status_history'::regclass
              AND p.polname=:policy
            """
        ),
        {"policy": _POLICY},
    ).mappings().one_or_none()
    if row is None or row["command"] != "r" or list(row["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("za07 notification history policy role/command drift")
    qualifier = row["qualifier"] or ""
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "org_branches",
    ):
        if token not in qualifier:
            raise RuntimeError(f"za07 notification history policy lost scope token: {token}")

    for role_name in _RUNTIME_ROLES:
        if bind.execute(
            sa.text("SELECT pg_catalog.has_table_privilege(:role,:relation,'SELECT')"),
            {"role": role_name, "relation": _HISTORY},
        ).scalar_one():
            raise RuntimeError(f"za07 leaked runtime history table SELECT: {role_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)

    op.execute(
        """
        CREATE POLICY p4c_notification_history_security_owner_select
        ON public.branch_status_history
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
                THEN EXISTS (
                    SELECT 1
                    FROM public.org_branches AS branch_data
                    WHERE branch_data.id=branch_status_history.branch_id
                      AND branch_data.org_id=CAST(
                          NULLIF(pg_catalog.current_setting('app.current_org_id',true),'') AS uuid
                      )
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
        "DROP POLICY IF EXISTS p4c_notification_history_security_owner_select "
        "ON public.branch_status_history"
    )
