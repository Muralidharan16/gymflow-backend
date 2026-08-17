"""Bind P4C notification recipient reads and child enqueue to worker context.

Revision ID: zb07d8e9f0a3c
Revises: za07d8e9f0a3b
Create Date: 2026-08-17

P4C notification fanout and delivery claims resolve recipients from the live
``members`` projection after establishing a worker-owned outbox lease.  The
members table is FORCE RLS and its tenant CRUD policies intentionally target
``app_runtime`` only.  Column-scoped SELECT granted to ``app_security_owner``
is therefore insufficient for the SECURITY DEFINER notification functions.

The same fanout creates canonical ``notification.delivery`` children in the
FORCE-RLS lifecycle outbox.  P4C already inherits the exact app_security_owner
column INSERT ACL, but FORCE RLS must independently authorize those child rows.

This still-uncertified corrective revision therefore adds two narrowly scoped
policies for ``app_security_owner``: tenant-bound member SELECT and canonical
notification-child INSERT.  Both require the exact lifecycle-worker session
context.  The INSERT policy additionally binds every child to an authoritative
notification command and to its live worker-owned ``branch.member_notification``
parent.  Runtime roles receive no new table privilege and cannot SET ROLE to the
security owner.
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
_OUTBOX = "public.branch_outbox_events"
_COMMANDS = "public.notification_commands"
_MEMBER_POLICY = "p4c_notification_member_security_owner_select"
_OUTBOX_POLICY = "p4c_notification_delivery_security_owner_insert"
_MEMBER_COLUMNS = (
    "id",
    "org_id",
    "home_branch_id",
    "name",
    "email",
    "status",
    "is_active",
)
_OUTBOX_INSERT_COLUMNS = (
    "outbox_id",
    "tenant_id",
    "branch_id",
    "event_type",
    "payload",
    "created_at",
    "process_after",
    "status",
    "attempt_count",
    "max_attempts",
    "correlation_id",
    "leased_by",
    "leased_until",
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


def _require_policy_absent(bind, relation: str, policy: str) -> None:
    if bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_policy "
            "WHERE polrelid=CAST(:relation AS regclass) AND polname=:policy)"
        ),
        {"relation": relation, "policy": policy},
    ).scalar_one():
        raise RuntimeError(f"zb07 notification policy collision: {policy}")


def _require_force_rls(bind, relation: str) -> None:
    enabled, forced = bind.execute(
        sa.text(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE oid=CAST(:relation AS regclass)"
        ),
        {"relation": relation},
    ).one()
    if not enabled or not forced:
        raise RuntimeError(f"zb07 requires {relation} ENABLE+FORCE RLS")


def _require_predecessor(bind) -> None:
    for relation in (_MEMBERS, _OUTBOX, _COMMANDS):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"zb07 missing predecessor relation {relation}")

    _require_force_rls(bind, _MEMBERS)
    _require_force_rls(bind, _OUTBOX)

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

    for column in _OUTBOX_INSERT_COLUMNS:
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,:column,'INSERT')"
            ),
            {"role": _SECURITY_OWNER, "relation": _OUTBOX, "column": column},
        ).scalar_one():
            raise RuntimeError(
                f"zb07 missing predecessor app_security_owner outbox column INSERT: {column}"
            )

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'SELECT')"
        ),
        {"role": _SECURITY_OWNER, "relation": _COMMANDS},
    ).scalar_one():
        raise RuntimeError("zb07 requires app_security_owner notification command SELECT")

    _require_policy_absent(bind, _MEMBERS, _MEMBER_POLICY)
    _require_policy_absent(bind, _OUTBOX, _OUTBOX_POLICY)


def _post_install_proof(bind) -> None:
    member = bind.execute(
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
        {"policy": _MEMBER_POLICY},
    ).mappings().one_or_none()
    if member is None or member["command"] != "r" or list(member["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification member policy role/command drift")
    member_qualifier = member["qualifier"] or ""
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "org_id",
    ):
        if token not in member_qualifier:
            raise RuntimeError(f"zb07 notification member policy lost scope token: {token}")

    outbox = bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid) AS check_qualifier,
                   ARRAY(
                       SELECT r.rolname::text
                       FROM pg_catalog.pg_roles r
                       WHERE r.oid=ANY(p.polroles)
                       ORDER BY r.rolname
                   ) AS roles
            FROM pg_catalog.pg_policy p
            WHERE p.polrelid='public.branch_outbox_events'::regclass
              AND p.polname=:policy
            """
        ),
        {"policy": _OUTBOX_POLICY},
    ).mappings().one_or_none()
    if outbox is None or outbox["command"] != "a" or list(outbox["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification outbox policy role/command drift")
    check_qualifier = outbox["check_qualifier"] or ""
    for token in (
        "notification.delivery",
        "notification_commands",
        "branch.member_notification",
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "command_id",
        "source_outbox_id",
    ):
        if token not in check_qualifier:
            raise RuntimeError(f"zb07 notification outbox policy lost scope token: {token}")


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

    op.execute(
        """
        CREATE POLICY p4c_notification_delivery_security_owner_insert
        ON public.branch_outbox_events
        FOR INSERT TO app_security_owner
        WITH CHECK (
            NULLIF(pg_catalog.current_setting('app.current_role',true),'') = 'branch_lifecycle_worker'
            AND NULLIF(pg_catalog.current_setting('app.internal_maintenance',true),'') = 'branch_lifecycle_saga'
            AND pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.worker_id',true),''),'uuid'
            )
            AND pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id',true),''),'uuid'
            )
            AND tenant_id=CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id',true),'') AS uuid
            )
            AND event_type='notification.delivery'
            AND status='pending'
            AND attempt_count=0
            AND leased_by IS NULL
            AND leased_until IS NULL
            AND payload=pg_catalog.jsonb_build_object('command_id',outbox_id::text)
            AND EXISTS (
                SELECT 1
                FROM public.notification_commands AS command_data
                JOIN public.branch_outbox_events AS parent_data
                  ON parent_data.outbox_id=command_data.source_outbox_id
                WHERE command_data.command_id=branch_outbox_events.outbox_id
                  AND command_data.tenant_id=branch_outbox_events.tenant_id
                  AND command_data.branch_id=branch_outbox_events.branch_id
                  AND command_data.correlation_id=branch_outbox_events.correlation_id
                  AND command_data.status='pending'
                  AND command_data.attempt_count=0
                  AND command_data.max_attempts=branch_outbox_events.max_attempts
                  AND command_data.next_attempt_at=branch_outbox_events.process_after
                  AND parent_data.tenant_id=branch_outbox_events.tenant_id
                  AND parent_data.branch_id=branch_outbox_events.branch_id
                  AND parent_data.correlation_id=branch_outbox_events.correlation_id
                  AND parent_data.event_type='branch.member_notification'
                  AND parent_data.status='processing'
                  AND parent_data.leased_by=CAST(
                      NULLIF(pg_catalog.current_setting('app.worker_id',true),'') AS uuid
                  )
                  AND parent_data.leased_until>pg_catalog.clock_timestamp()
            )
        )
        """
    )
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_delivery_security_owner_insert "
        "ON public.branch_outbox_events"
    )
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_member_security_owner_select "
        "ON public.members"
    )
