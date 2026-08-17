"""Bind P4C notification source reads and child enqueue under FORCE RLS.

Revision ID: zb07d8e9f0a3c
Revises: za07d8e9f0a3b
Create Date: 2026-08-17

P4C notification fanout and delivery claims resolve recipients from the live
``members`` projection after establishing a worker-owned outbox lease. The
members table is FORCE RLS and its tenant CRUD policies intentionally target
``app_runtime`` only. Column-scoped SELECT granted to ``app_security_owner`` is
therefore insufficient without a bounded security-owner policy.

P4C also creates durable notification.delivery and notification.reconcile child
work in the FORCE-RLS lifecycle outbox. Existing lifecycle infrastructure gives
``app_security_owner`` a bounded outbox read capability but intentionally did
not include the payload column. Reconciliation is the first P4C capability that
must read ``payload->>'command_id'`` from a live worker-owned outbox lease, so
this still-uncertified boundary adds SELECT on exactly ``payload`` to the
no-login security owner. Safe operator replay also resets the durable outbox
retry ceiling through a SECURITY DEFINER capability, so this boundary adds
UPDATE on exactly ``max_attempts`` to the same no-login owner. Runtime roles
receive no new table privilege and cannot SET ROLE to the security owner.
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
_DELIVERY_OUTBOX_POLICY = "p4c_notification_delivery_security_owner_insert"
_RECONCILE_OUTBOX_POLICY = "p4c_notification_reconcile_security_owner_insert"
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


def _has_column_privilege(bind, relation: str, column: str, privilege: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,:column,:privilege)"
            ),
            {
                "role": _SECURITY_OWNER,
                "relation": relation,
                "column": column,
                "privilege": privilege,
            },
        ).scalar_one()
    )


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
        if not _has_column_privilege(bind, _MEMBERS, column, "SELECT"):
            raise RuntimeError(
                f"zb07 missing P4C predecessor app_security_owner member column SELECT: {column}"
            )

    for column in _OUTBOX_INSERT_COLUMNS:
        if not _has_column_privilege(bind, _OUTBOX, column, "INSERT"):
            raise RuntimeError(
                f"zb07 missing predecessor app_security_owner outbox column INSERT: {column}"
            )

    if _has_column_privilege(bind, _OUTBOX, "payload", "SELECT"):
        raise RuntimeError(
            "zb07 refuses ambiguous predecessor app_security_owner outbox payload SELECT"
        )
    if _has_column_privilege(bind, _OUTBOX, "max_attempts", "UPDATE"):
        raise RuntimeError(
            "zb07 refuses ambiguous predecessor app_security_owner outbox max_attempts UPDATE"
        )

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege(:role,CAST(:relation AS regclass),'SELECT')"
        ),
        {"role": _SECURITY_OWNER, "relation": _COMMANDS},
    ).scalar_one():
        raise RuntimeError("zb07 requires app_security_owner notification command SELECT")

    _require_policy_absent(bind, _MEMBERS, _MEMBER_POLICY)
    _require_policy_absent(bind, _OUTBOX, _DELIVERY_OUTBOX_POLICY)
    _require_policy_absent(bind, _OUTBOX, _RECONCILE_OUTBOX_POLICY)


def _policy_row(bind, relation: str, policy: str):
    return bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   pg_catalog.pg_get_expr(p.polqual,p.polrelid) AS qualifier,
                   pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid) AS check_qualifier,
                   ARRAY(
                       SELECT r.rolname::text
                       FROM pg_catalog.pg_roles r
                       WHERE r.oid=ANY(p.polroles)
                       ORDER BY r.rolname
                   ) AS roles
            FROM pg_catalog.pg_policy p
            WHERE p.polrelid=CAST(:relation AS regclass)
              AND p.polname=:policy
            """
        ),
        {"relation": relation, "policy": policy},
    ).mappings().one_or_none()


def _post_install_proof(bind) -> None:
    if not _has_column_privilege(bind, _OUTBOX, "payload", "SELECT"):
        raise RuntimeError("zb07 app_security_owner outbox payload SELECT was not installed")
    if not _has_column_privilege(bind, _OUTBOX, "max_attempts", "UPDATE"):
        raise RuntimeError("zb07 app_security_owner outbox max_attempts UPDATE was not installed")

    # Runtime outbox privileges predate P4C and are certified by their owning
    # migrations. Do not infer a predecessor ACL contract here. The P4C delta
    # is proven as exact column grants to app_security_owner, while the identity
    # proof above guarantees no runtime can SET ROLE to that owner.
    member = _policy_row(bind, _MEMBERS, _MEMBER_POLICY)
    if member is None or member["command"] != "r" or list(member["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification member policy role/command drift")
    for token in (
        "app.current_role",
        "branch_lifecycle_worker",
        "app.internal_maintenance",
        "branch_lifecycle_saga",
        "app.current_org_id",
        "app.worker_id",
        "org_id",
    ):
        if token not in (member["qualifier"] or ""):
            raise RuntimeError(f"zb07 notification member policy lost scope token: {token}")

    delivery = _policy_row(bind, _OUTBOX, _DELIVERY_OUTBOX_POLICY)
    if delivery is None or delivery["command"] != "a" or list(delivery["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification delivery outbox policy role/command drift")
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
        if token not in (delivery["check_qualifier"] or ""):
            raise RuntimeError(f"zb07 notification delivery policy lost scope token: {token}")

    reconcile = _policy_row(bind, _OUTBOX, _RECONCILE_OUTBOX_POLICY)
    if reconcile is None or reconcile["command"] != "a" or list(reconcile["roles"]) != [_SECURITY_OWNER]:
        raise RuntimeError("zb07 notification reconcile outbox policy role/command drift")
    for token in (
        "notification.reconcile",
        "notification_commands",
        "provider_accepted",
        "resend",
        "provider_reference_id",
        "acknowledged_at",
        "app.current_role",
        "lifecycle_maintenance",
        "app.internal_maintenance",
        "lifecycle",
        "command_id",
    ):
        if token not in (reconcile["check_qualifier"] or ""):
            raise RuntimeError(f"zb07 notification reconcile policy lost scope token: {token}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)

    op.execute(
        "GRANT SELECT (payload) ON TABLE public.branch_outbox_events TO app_security_owner"
    )
    op.execute(
        "GRANT UPDATE (max_attempts) ON TABLE public.branch_outbox_events TO app_security_owner"
    )

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

    op.execute(
        """
        CREATE POLICY p4c_notification_reconcile_security_owner_insert
        ON public.branch_outbox_events
        FOR INSERT TO app_security_owner
        WITH CHECK (
            NULLIF(pg_catalog.current_setting('app.current_role',true),'') = 'lifecycle_maintenance'
            AND NULLIF(pg_catalog.current_setting('app.internal_maintenance',true),'') = 'lifecycle'
            AND event_type='notification.reconcile'
            AND status='pending'
            AND attempt_count=0
            AND max_attempts=8
            AND leased_by IS NULL
            AND leased_until IS NULL
            AND pg_catalog.pg_input_is_valid(NULLIF(payload->>'command_id',''),'uuid')
            AND payload=pg_catalog.jsonb_build_object('command_id',payload->>'command_id')
            AND EXISTS (
                SELECT 1
                FROM public.notification_commands AS command_data
                WHERE command_data.command_id=CAST(NULLIF(branch_outbox_events.payload->>'command_id','') AS uuid)
                  AND command_data.tenant_id=branch_outbox_events.tenant_id
                  AND command_data.branch_id=branch_outbox_events.branch_id
                  AND command_data.correlation_id=branch_outbox_events.correlation_id
                  AND command_data.status='provider_accepted'
                  AND command_data.provider_code='resend'
                  AND command_data.provider_reference_id IS NOT NULL
                  AND command_data.acknowledged_at IS NOT NULL
                  AND command_data.acknowledged_at<=pg_catalog.clock_timestamp()-INTERVAL '2 minutes'
            )
        )
        """
    )
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_reconcile_security_owner_insert "
        "ON public.branch_outbox_events"
    )
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_delivery_security_owner_insert "
        "ON public.branch_outbox_events"
    )
    op.execute(
        "DROP POLICY IF EXISTS p4c_notification_member_security_owner_select "
        "ON public.members"
    )
    op.execute(
        "REVOKE UPDATE (max_attempts) ON TABLE public.branch_outbox_events FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT (payload) ON TABLE public.branch_outbox_events FROM app_security_owner"
    )