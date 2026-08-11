"""Establish a leased, tenant-bound worker boundary for branch lifecycle sagas.

Revision ID: 2c3d4e5f6071
Revises: 1b2c3d4e5f60
Create Date: 2026-08-11

Branch lifecycle Transaction A is durable, but Transaction B was launched via
FastAPI BackgroundTasks on the request-scoped AsyncSession. The existing
branch-outbox poller also used ordinary application credentials and treated
mock log statements as successful external delivery.

This revision makes ``branch_outbox_events`` a tenant-aware leased queue and
adds only the worker capabilities required to execute a lifecycle saga under a
live ``branch.lifecycle_saga`` lease. Worker source/state policies require the
persisted tenant, branch, correlation id and worker lease to match the current
transaction context. External command delivery remains outbox data; no external
integration is declared successful by this migration.

The dedicated worker retains NOLOGIN/NOBYPASSRLS/no-schema-CREATE posture and
receives no tenant-root/auth credential capability. DELETE/TRUNCATE/REFERENCES/
TRIGGER remain forbidden.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "2c3d4e5f6071"
down_revision = "1b2c3d4e5f60"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_WORKER = "worker_runtime"
_OUTBOX = "public.branch_outbox_events"
_MAINTENANCE_TOKEN = "branch_lifecycle_saga"

_CATALOGS = (
    "public.branch_status_definitions",
    "public.branch_status_transitions",
    "public.branch_deactivation_policies",
)
_APPEND_TABLES = (
    "public.branch_status_history",
    "public.branch_lifecycle_events",
    "public.branch_watchdog_alerts",
)

_OUTBOX_UPDATE_COLUMNS = (
    "status",
    "attempt_count",
    "last_attempted_at",
    "last_error",
    "leased_by",
    "leased_until",
    "process_after",
)


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
        raise RuntimeError("2c3d lifecycle worker migration requires migration_owner")
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
        raise RuntimeError("migration_owner violates the reduced role contract")

    worker = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _WORKER},
    ).one_or_none()
    if worker is None:
        raise RuntimeError("2c3d requires managed worker_runtime")
    if any(bool(value) for value in worker):
        raise RuntimeError("worker_runtime violates NOLOGIN/NOINHERIT/NOBYPASSRLS")
    if bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"),
        {"member": _MIGRATION_OWNER, "role": _WORKER},
    ).scalar_one():
        raise RuntimeError("migration_owner must not be a worker_runtime member")


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                "SELECT polname::text FROM pg_catalog.pg_policy "
                "WHERE polrelid = CAST(:relation AS regclass)"
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _uuid_guc(name: str) -> str:
    return f"""
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('{name}', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('{name}', true), '') AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _maintenance() -> str:
    return (
        "NULLIF(pg_catalog.current_setting('app.internal_maintenance', true), '') "
        f"= '{_MAINTENANCE_TOKEN}'"
    )


def _live_saga_lease(target_org: str, target_branch: str, target_correlation: str | None = None) -> str:
    worker_id = _uuid_guc("app.worker_id")
    current_org = _uuid_guc("app.current_org_id")
    correlation = "TRUE"
    if target_correlation is not None:
        correlation = f"lease_data.correlation_id = {target_correlation}"
    return f"""
        (
            {target_org} = {current_org}
            AND {_maintenance()}
            AND EXISTS (
                SELECT 1
                FROM public.branch_outbox_events AS lease_data
                WHERE lease_data.tenant_id = {target_org}
                  AND lease_data.branch_id = {target_branch}
                  AND lease_data.event_type = 'branch.lifecycle_saga'
                  AND lease_data.status = 'processing'
                  AND lease_data.leased_by = {worker_id}
                  AND lease_data.leased_until > pg_catalog.clock_timestamp()
                  AND {correlation}
            )
        )
    """


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    required = (
        _OUTBOX,
        "public.organizations",
        "public.org_branches",
        "public.org_branch_state",
        *_CATALOGS,
        *_APPEND_TABLES,
    )
    missing = bind.execute(
        sa.text(
            """
            SELECT relation_name
            FROM unnest(CAST(:relations AS text[])) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY relation_name
            """
        ),
        {"relations": list(required)},
    ).scalars().all()
    if missing:
        raise RuntimeError(f"2c3d required relations missing: {tuple(missing)!r}")

    existing_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT attname::text
                FROM pg_catalog.pg_attribute
                WHERE attrelid = CAST(:relation AS regclass)
                  AND attnum > 0 AND NOT attisdropped
                  AND attname = ANY(CAST(:columns AS text[]))
                """
            ),
            {
                "relation": _OUTBOX,
                "columns": ["tenant_id", "leased_by", "leased_until"],
            },
        ).scalars().all()
    )
    if existing_columns:
        raise RuntimeError(
            f"2c3d refuses pre-existing lifecycle queue columns: {sorted(existing_columns)!r}"
        )

    # Every predecessor row must resolve to one canonical tenant before making
    # tenant identity mandatory.
    invalid = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.branch_outbox_events AS outbox_data
            LEFT JOIN public.org_branches AS branch_data
              ON branch_data.id = outbox_data.branch_id
            WHERE branch_data.id IS NULL
            """
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(
            f"2c3d lifecycle outbox contains rows with invalid branch lineage: count={invalid}"
        )

    collisions = {
        "public.org_branches": {"lifecycle_worker_branch_read"},
        "public.org_branch_state": {"lifecycle_worker_state_read", "lifecycle_worker_state_update"},
        "public.branch_status_history": {"lifecycle_worker_history_insert"},
        "public.branch_lifecycle_events": {"lifecycle_worker_event_insert"},
        "public.branch_watchdog_alerts": {"lifecycle_worker_watchdog_insert"},
        _OUTBOX: {
            "lifecycle_worker_outbox_select",
            "lifecycle_worker_outbox_update",
            "lifecycle_worker_outbox_insert",
        },
    }
    for relation, names in collisions.items():
        present = _policy_names(bind, relation) & names
        if present:
            raise RuntimeError(
                f"2c3d policy collision on {relation}: {sorted(present)!r}"
            )

    op.execute(
        """
        ALTER TABLE public.branch_outbox_events
            ADD COLUMN tenant_id uuid,
            ADD COLUMN leased_by uuid,
            ADD COLUMN leased_until timestamptz
        """
    )
    op.execute(
        """
        UPDATE public.branch_outbox_events AS outbox_data
        SET tenant_id = branch_data.org_id
        FROM public.org_branches AS branch_data
        WHERE branch_data.id = outbox_data.branch_id
        """
    )
    op.execute(
        """
        ALTER TABLE public.branch_outbox_events
            ALTER COLUMN tenant_id SET NOT NULL,
            ADD CONSTRAINT fk_branch_outbox_tenant
                FOREIGN KEY (tenant_id) REFERENCES public.organizations(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT chk_branch_outbox_attempt_bounds
                CHECK (max_attempts BETWEEN 1 AND 20 AND attempt_count BETWEEN 0 AND max_attempts),
            ADD CONSTRAINT chk_branch_outbox_lease_state
                CHECK (
                    (status = 'processing') =
                    (leased_by IS NOT NULL AND leased_until IS NOT NULL)
                )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_branch_outbox_ready_claim
        ON public.branch_outbox_events (process_after, created_at, outbox_id)
        WHERE status = 'pending'
        """
    )

    for relation in _CATALOGS:
        op.execute(f"GRANT SELECT ON TABLE {relation} TO worker_runtime")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.org_branch_state TO worker_runtime")
    op.execute("GRANT SELECT ON TABLE public.org_branches TO worker_runtime")
    op.execute("GRANT INSERT ON TABLE public.branch_status_history TO worker_runtime")
    op.execute("GRANT INSERT ON TABLE public.branch_lifecycle_events TO worker_runtime")
    op.execute("GRANT INSERT ON TABLE public.branch_watchdog_alerts TO worker_runtime")
    op.execute("GRANT SELECT, INSERT ON TABLE public.branch_outbox_events TO worker_runtime")
    op.execute(
        "GRANT UPDATE (status, attempt_count, last_attempted_at, last_error, "
        "leased_by, leased_until, process_after) "
        "ON TABLE public.branch_outbox_events TO worker_runtime"
    )

    # Queue claim/update is cross-tenant by design for the dedicated internal
    # worker. Source/state access below becomes branch/tenant/lease bound.
    op.execute(
        "CREATE POLICY lifecycle_worker_outbox_select ON public.branch_outbox_events "
        "FOR SELECT TO worker_runtime USING (TRUE)"
    )
    op.execute(
        "CREATE POLICY lifecycle_worker_outbox_update ON public.branch_outbox_events "
        "FOR UPDATE TO worker_runtime USING (TRUE) WITH CHECK (TRUE)"
    )

    current_org = _uuid_guc("app.current_org_id")
    current_worker = _uuid_guc("app.worker_id")
    op.execute(
        f"""
        CREATE POLICY lifecycle_worker_outbox_insert
        ON public.branch_outbox_events
        FOR INSERT TO worker_runtime
        WITH CHECK (
            tenant_id = {current_org}
            AND status = 'pending'
            AND attempt_count = 0
            AND leased_by IS NULL
            AND leased_until IS NULL
            AND event_type IN (
                'branch.search_deindex',
                'branch.search_index',
                'branch.member_notification',
                'branch.refund_required'
            )
            AND EXISTS (
                SELECT 1
                FROM public.branch_outbox_events AS lease_data
                WHERE lease_data.tenant_id = branch_outbox_events.tenant_id
                  AND lease_data.branch_id = branch_outbox_events.branch_id
                  AND lease_data.correlation_id = branch_outbox_events.correlation_id
                  AND lease_data.event_type = 'branch.lifecycle_saga'
                  AND lease_data.status = 'processing'
                  AND lease_data.leased_by = {current_worker}
                  AND lease_data.leased_until > pg_catalog.clock_timestamp()
            )
        )
        """
    )

    branch_scope = _live_saga_lease("org_id", "id")
    op.execute(
        f"CREATE POLICY lifecycle_worker_branch_read ON public.org_branches "
        f"FOR SELECT TO worker_runtime USING ({branch_scope})"
    )
    state_scope = _live_saga_lease("org_id", "branch_id")
    op.execute(
        f"CREATE POLICY lifecycle_worker_state_read ON public.org_branch_state "
        f"FOR SELECT TO worker_runtime USING ({state_scope})"
    )
    op.execute(
        f"CREATE POLICY lifecycle_worker_state_update ON public.org_branch_state "
        f"FOR UPDATE TO worker_runtime USING ({state_scope}) WITH CHECK ({state_scope})"
    )

    history_scope = _live_saga_lease(
        "(SELECT branch_data.org_id FROM public.org_branches AS branch_data "
        "WHERE branch_data.id = branch_status_history.branch_id)",
        "branch_status_history.branch_id",
        "branch_status_history.correlation_id",
    )
    op.execute(
        f"CREATE POLICY lifecycle_worker_history_insert ON public.branch_status_history "
        f"FOR INSERT TO worker_runtime WITH CHECK ({history_scope})"
    )
    event_scope = _live_saga_lease(
        "(SELECT branch_data.org_id FROM public.org_branches AS branch_data "
        "WHERE branch_data.id = branch_lifecycle_events.branch_id)",
        "branch_lifecycle_events.branch_id",
        "branch_lifecycle_events.correlation_id",
    )
    op.execute(
        f"CREATE POLICY lifecycle_worker_event_insert ON public.branch_lifecycle_events "
        f"FOR INSERT TO worker_runtime WITH CHECK ({event_scope})"
    )
    watchdog_scope = _live_saga_lease(
        "(SELECT branch_data.org_id FROM public.org_branches AS branch_data "
        "WHERE branch_data.id = branch_watchdog_alerts.branch_id)",
        "branch_watchdog_alerts.branch_id",
    )
    op.execute(
        f"CREATE POLICY lifecycle_worker_watchdog_insert ON public.branch_watchdog_alerts "
        f"FOR INSERT TO worker_runtime WITH CHECK ({watchdog_scope})"
    )

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.organizations', 'SELECT') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.organizations', 'UPDATE') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.organizations', 'INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("2c3d worker leaked tenant-root capability")
    if bind.execute(
        sa.text("SELECT pg_catalog.has_schema_privilege('worker_runtime', 'public', 'CREATE')")
    ).scalar_one():
        raise RuntimeError("2c3d worker leaked schema CREATE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    pending_saga = bind.execute(
        sa.text(
            "SELECT count(*) FROM public.branch_outbox_events "
            "WHERE event_type = 'branch.lifecycle_saga' "
            "AND status IN ('pending', 'processing')"
        )
    ).scalar_one()
    if pending_saga:
        raise RuntimeError(
            "2c3d downgrade refuses live lifecycle saga work that predecessor "
            f"BackgroundTasks semantics cannot represent: count={pending_saga}"
        )

    for relation, policy_name in (
        ("public.org_branches", "lifecycle_worker_branch_read"),
        ("public.org_branch_state", "lifecycle_worker_state_read"),
        ("public.org_branch_state", "lifecycle_worker_state_update"),
        ("public.branch_status_history", "lifecycle_worker_history_insert"),
        ("public.branch_lifecycle_events", "lifecycle_worker_event_insert"),
        ("public.branch_watchdog_alerts", "lifecycle_worker_watchdog_insert"),
        (_OUTBOX, "lifecycle_worker_outbox_insert"),
        (_OUTBOX, "lifecycle_worker_outbox_update"),
        (_OUTBOX, "lifecycle_worker_outbox_select"),
    ):
        op.execute(f"DROP POLICY {policy_name} ON {relation}")

    for relation in _CATALOGS:
        op.execute(f"REVOKE SELECT ON TABLE {relation} FROM worker_runtime")
    op.execute("REVOKE SELECT, UPDATE ON TABLE public.org_branch_state FROM worker_runtime")
    op.execute("REVOKE SELECT ON TABLE public.org_branches FROM worker_runtime")
    op.execute("REVOKE INSERT ON TABLE public.branch_status_history FROM worker_runtime")
    op.execute("REVOKE INSERT ON TABLE public.branch_lifecycle_events FROM worker_runtime")
    op.execute("REVOKE INSERT ON TABLE public.branch_watchdog_alerts FROM worker_runtime")
    op.execute("REVOKE SELECT, INSERT ON TABLE public.branch_outbox_events FROM worker_runtime")
    op.execute(
        "REVOKE UPDATE (status, attempt_count, last_attempted_at, last_error, "
        "leased_by, leased_until, process_after) "
        "ON TABLE public.branch_outbox_events FROM worker_runtime"
    )

    op.execute("DROP INDEX public.ix_branch_outbox_ready_claim")
    op.execute(
        """
        ALTER TABLE public.branch_outbox_events
            DROP CONSTRAINT chk_branch_outbox_lease_state,
            DROP CONSTRAINT chk_branch_outbox_attempt_bounds,
            DROP CONSTRAINT fk_branch_outbox_tenant,
            DROP COLUMN leased_until,
            DROP COLUMN leased_by,
            DROP COLUMN tenant_id
        """
    )
