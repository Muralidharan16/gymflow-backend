"""Scope lifecycle RLS policies to their intended database identities.

Revision ID: 718293a4b5c6
Revises: 60718293a4b5
Create Date: 2026-08-11

The lifecycle policies introduced by 708/92 were created without ``TO`` and
therefore applied to PUBLIC.  After the dedicated worker boundary was added,
PostgreSQL composed those ordinary API policies into worker queries as well.
In particular, ``p_outbox_select`` resolves tenant ownership through
``org_branches`` while ``lifecycle_worker_branch_read`` resolves its lease
through ``branch_outbox_events``, creating the RLS rewrite cycle:

    org_branches -> branch_outbox_events -> org_branches

This revision changes no policy predicates and grants no new table capability.
It only assigns each existing policy to the database identity that already owns
the corresponding ACL: application read/update/append policies to app_runtime,
and onboarding branch-state INSERT to auth_runtime.  Dedicated worker and
security-owner policies remain separate and are no longer polluted by PUBLIC
application policy composition.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "718293a4b5c6"
down_revision = "60718293a4b5"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"

_POLICY_RELATIONS = {
    "public.org_branch_state": {
        "p_branch_select": "app_runtime",
        "p_branch_update": "app_runtime",
        "p_branch_insert": "auth_runtime",
        "p_branch_delete": "app_runtime",
    },
    "public.branch_status_history": {
        "p_history_select": "app_runtime",
        "p_history_insert": "app_runtime",
    },
    "public.branch_lifecycle_events": {
        "p_events_select": "app_runtime",
        "p_events_insert": "app_runtime",
    },
    "public.branch_outbox_events": {
        "p_outbox_select": "app_runtime",
        "p_outbox_insert": "app_runtime",
    },
    "public.branch_watchdog_alerts": {
        "p_watchdog_select": "app_runtime",
        "p_watchdog_insert": "app_runtime",
    },
}


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
        raise RuntimeError("718293 lifecycle policy scoping requires migration_owner")
    if any(
        bool(row[name])
        for name in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _policy_row(bind, relation: str, policy_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   p.polpermissive,
                   p.polroles,
                   pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS using_expr,
                   pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
            FROM pg_catalog.pg_policy AS p
            WHERE p.polrelid = CAST(:relation AS regclass)
              AND p.polname = :policy_name
            """
        ),
        {"relation": relation, "policy_name": policy_name},
    ).mappings().one_or_none()


def _require_public_predecessor(bind) -> None:
    for relation, policies in _POLICY_RELATIONS.items():
        for policy_name in policies:
            row = _policy_row(bind, relation, policy_name)
            if row is None:
                raise RuntimeError(
                    f"718293 predecessor policy missing: {relation}.{policy_name}"
                )
            if list(row["polroles"]) != [0]:
                raise RuntimeError(
                    "718293 refuses policy-role drift before scoping: "
                    f"{relation}.{policy_name}: roles={list(row['polroles'])!r}"
                )


def _drop_target_policies() -> None:
    for relation, policies in _POLICY_RELATIONS.items():
        for policy_name in policies:
            op.execute(f"DROP POLICY {policy_name} ON {relation}")


def _create_scoped_policies() -> None:
    tenant = """
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')
                AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """

    # Preserve the exact 92a application visibility matrix; only TO changes.
    op.execute(
        f"""
        CREATE POLICY p_branch_select ON public.org_branch_state
        FOR SELECT TO app_runtime
        USING (
            org_id = {tenant}
            AND (
                (auth.role() = 'trainer' AND status = 'active')
                OR (
                    auth.role() = 'manager'
                    AND status IN ('active','temporarily_closed','under_renovation')
                )
                OR (
                    auth.role() IN ('owner','admin','org_admin')
                    AND status IN (
                        'active','temporarily_closed','under_renovation',
                        'compliance_suspended','permanently_closed'
                    )
                )
                OR (
                    auth.role() IN (
                        'compliance','superadmin','system',
                        'saga_orchestrator','system_watchdog'
                    )
                    AND status IN (
                        'active','temporarily_closed','under_renovation',
                        'compliance_suspended','permanently_closed'
                    )
                )
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_update ON public.org_branch_state
        FOR UPDATE TO app_runtime
        USING (
            org_id = {tenant}
            AND auth.role() IN (
                'owner','admin','org_admin','compliance','superadmin',
                'system','saga_orchestrator','system_watchdog'
            )
        )
        WITH CHECK (
            org_id = {tenant}
            AND auth.role() IN (
                'owner','admin','org_admin','compliance','superadmin',
                'system','saga_orchestrator','system_watchdog'
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_insert ON public.org_branch_state
        FOR INSERT TO auth_runtime
        WITH CHECK (
            org_id = {tenant}
            AND auth.role() IN ('owner','admin','org_admin','superadmin','system')
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_delete ON public.org_branch_state
        FOR DELETE TO app_runtime
        USING (org_id = {tenant} AND auth.role() = 'superadmin')
        """
    )

    append_roles = (
        "'owner','admin','org_admin','compliance','superadmin',"
        "'system','saga_orchestrator','system_watchdog'"
    )
    for relation, prefix in (
        ("public.branch_status_history", "history"),
        ("public.branch_lifecycle_events", "events"),
        ("public.branch_outbox_events", "outbox"),
        ("public.branch_watchdog_alerts", "watchdog"),
    ):
        short_name = relation.split(".", 1)[1]
        tenant_expr = (
            "EXISTS (SELECT 1 FROM public.org_branches AS tenant_branch "
            f"WHERE tenant_branch.id = {short_name}.branch_id "
            f"AND tenant_branch.org_id = {tenant})"
        )
        op.execute(
            f"""
            CREATE POLICY p_{prefix}_select ON {relation}
            FOR SELECT TO app_runtime
            USING ({tenant_expr} AND auth.role() IN ({append_roles}))
            """
        )
        op.execute(
            f"""
            CREATE POLICY p_{prefix}_insert ON {relation}
            FOR INSERT TO app_runtime
            WITH CHECK ({tenant_expr} AND auth.role() IN ({append_roles}))
            """
        )


def _create_public_predecessor() -> None:
    # Downgrade restores exactly the public role scope represented by 607's
    # predecessor. Predicates remain the same as the forward definitions above.
    tenant = """
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            )
            THEN CAST(NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid)
            ELSE CAST(NULL AS uuid)
        END
    """
    op.execute(
        f"""
        CREATE POLICY p_branch_select ON public.org_branch_state FOR SELECT USING (
            org_id = {tenant} AND (
                (auth.role() = 'trainer' AND status = 'active') OR
                (auth.role() = 'manager' AND status IN ('active','temporarily_closed','under_renovation')) OR
                (auth.role() IN ('owner','admin','org_admin') AND status IN ('active','temporarily_closed','under_renovation','compliance_suspended','permanently_closed')) OR
                (auth.role() IN ('compliance','superadmin','system','saga_orchestrator','system_watchdog') AND status IN ('active','temporarily_closed','under_renovation','compliance_suspended','permanently_closed'))
            )
        )
        """
    )
    roles = "'owner','admin','org_admin','compliance','superadmin','system','saga_orchestrator','system_watchdog'"
    op.execute(
        f"CREATE POLICY p_branch_update ON public.org_branch_state FOR UPDATE "
        f"USING (org_id = {tenant} AND auth.role() IN ({roles})) "
        f"WITH CHECK (org_id = {tenant} AND auth.role() IN ({roles}))"
    )
    op.execute(
        f"CREATE POLICY p_branch_insert ON public.org_branch_state FOR INSERT "
        f"WITH CHECK (org_id = {tenant} AND auth.role() IN ('owner','admin','org_admin','superadmin','system'))"
    )
    op.execute(
        f"CREATE POLICY p_branch_delete ON public.org_branch_state FOR DELETE "
        f"USING (org_id = {tenant} AND auth.role() = 'superadmin')"
    )
    for relation, prefix in (
        ("public.branch_status_history", "history"),
        ("public.branch_lifecycle_events", "events"),
        ("public.branch_outbox_events", "outbox"),
        ("public.branch_watchdog_alerts", "watchdog"),
    ):
        short_name = relation.split(".", 1)[1]
        tenant_expr = (
            "EXISTS (SELECT 1 FROM public.org_branches AS tenant_branch "
            f"WHERE tenant_branch.id = {short_name}.branch_id "
            f"AND tenant_branch.org_id = {tenant})"
        )
        op.execute(
            f"CREATE POLICY p_{prefix}_select ON {relation} FOR SELECT "
            f"USING ({tenant_expr} AND auth.role() IN ({roles}))"
        )
        op.execute(
            f"CREATE POLICY p_{prefix}_insert ON {relation} FOR INSERT "
            f"WITH CHECK ({tenant_expr} AND auth.role() IN ({roles}))"
        )


def _verify_scoped(bind) -> None:
    role_oids = {
        name: bind.execute(
            sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :name"),
            {"name": name},
        ).scalar_one()
        for name in ("app_runtime", "auth_runtime", "worker_runtime", "app_security_owner")
    }
    for relation, policies in _POLICY_RELATIONS.items():
        for policy_name, role_name in policies.items():
            row = _policy_row(bind, relation, policy_name)
            if row is None or list(row["polroles"]) != [role_oids[role_name]]:
                raise RuntimeError(
                    "718293 policy scope postcondition failed: "
                    f"{relation}.{policy_name}: expected={role_name}, "
                    f"observed={None if row is None else list(row['polroles'])!r}"
                )

    # Worker and security owner must still have their dedicated policies; this
    # revision does not replace or broaden them.
    dedicated = {
        "public.org_branches": (
            "branch_hours_worker_branch_read",
            "lifecycle_worker_branch_read",
            "branch_hours_internal_enqueue_branch_read",
        ),
        "public.branch_outbox_events": (
            "lifecycle_worker_outbox_select",
            "lifecycle_worker_outbox_update",
            "lifecycle_internal_outbox_read",
            "lifecycle_internal_child_insert",
        ),
    }
    for relation, names in dedicated.items():
        observed = set(
            bind.execute(
                sa.text(
                    "SELECT polname::text FROM pg_catalog.pg_policy "
                    "WHERE polrelid = CAST(:relation AS regclass)"
                ),
                {"relation": relation},
            ).scalars().all()
        )
        missing = set(names) - observed
        if missing:
            raise RuntimeError(
                f"718293 dedicated policy drift on {relation}: missing={sorted(missing)!r}"
            )

    for role_name in ("worker_runtime", "app_security_owner"):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege(CAST(:role_name AS name), 'public', 'CREATE')"
            ),
            {"role_name": role_name},
        ).scalar_one():
            raise RuntimeError(f"718293 leaked public CREATE to {role_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_public_predecessor(bind)
    _drop_target_policies()
    _create_scoped_policies()
    _verify_scoped(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _verify_scoped(bind)
    _drop_target_policies()
    _create_public_predecessor()
    _require_public_predecessor(bind)
