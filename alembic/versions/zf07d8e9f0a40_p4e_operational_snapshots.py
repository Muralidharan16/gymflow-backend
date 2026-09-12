"""Expose aggregate-only P4E search/refund operational snapshots.

Revision ID: zf07d8e9f0a40
Revises: ze07d8e9f0a3f
Create Date: 2026-09-12

P4E adds observation only. The two no-argument SECURITY DEFINER functions
return bounded-cardinality numeric aggregates to the existing isolated
lifecycle maintenance capability. They expose no tenant/entity identifiers,
perform no mutation, and grant no new table authority to runtime identities.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "zf07d8e9f0a40"
down_revision = "ze07d8e9f0a3f"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_APP_SECURE_SCHEMA = "app_secure"

_SEARCH_SNAPSHOT_NAME = "search_operational_snapshot"
_REFUND_SNAPSHOT_NAME = "refund_execution_operational_snapshot"
_SEARCH_SNAPSHOT = f"{_APP_SECURE_SCHEMA}.{_SEARCH_SNAPSHOT_NAME}()"
_REFUND_SNAPSHOT = f"{_APP_SECURE_SCHEMA}.{_REFUND_SNAPSHOT_NAME}()"


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(row) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("zf07 P4E migration requires migration_owner")


def _function_oid(bind, function_name: str, argument_count: int = 0):
    return bind.execute(
        sa.text(
            """
            SELECT p.oid
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = p.pronamespace
            WHERE n.nspname = :schema_name
              AND p.proname = :function_name
              AND p.pronargs = :argument_count
            """
        ),
        {
            "schema_name": _APP_SECURE_SCHEMA,
            "function_name": function_name,
            "argument_count": argument_count,
        },
    ).scalar_one_or_none()


def _relation_oid(bind, relation: str):
    schema_name, relation_name = relation.split(".", 1)
    return bind.execute(
        sa.text(
            """
            SELECT c.oid
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            WHERE n.nspname = :schema_name
              AND c.relname = :relation_name
            """
        ),
        {
            "schema_name": schema_name,
            "relation_name": relation_name,
        },
    ).scalar_one_or_none()


def _has_table_select(bind, relation: str) -> bool:
    relation_oid = _relation_oid(bind, relation)
    if relation_oid is None:
        return False
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_table_privilege(
                    :role, CAST(:relation_oid AS oid), 'SELECT'
                )
                """
            ),
            {
                "role": _SECURITY_OWNER,
                "relation_oid": relation_oid,
            },
        ).scalar_one()
    )


def _has_column_select(bind, relation: str, column: str) -> bool:
    relation_oid = _relation_oid(bind, relation)
    if relation_oid is None:
        return False
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_column_privilege(
                    :role, CAST(:relation_oid AS oid), :column, 'SELECT'
                )
                """
            ),
            {
                "role": _SECURITY_OWNER,
                "relation_oid": relation_oid,
                "column": column,
            },
        ).scalar_one()
    )


def _require_upgrade_preconditions(bind) -> None:
    for function_name, signature in (
        (_SEARCH_SNAPSHOT_NAME, _SEARCH_SNAPSHOT),
        (_REFUND_SNAPSHOT_NAME, _REFUND_SNAPSHOT),
    ):
        if _function_oid(bind, function_name) is not None:
            raise RuntimeError(
                f"zf07 P4E snapshot unexpectedly already exists: {signature}"
            )

    # P4E must consume only authority already owned by the certified P4B/P4D
    # lineage. In particular, zc07 already grants app_security_owner full
    # SELECT on branch_outbox_events and finance.refunds, while u07 provides
    # the search-state columns. P4E deliberately adds zero direct table ACLs.
    for relation in (
        "public.branch_outbox_events",
        "finance.refunds",
        "finance.refund_execution_commands",
    ):
        if not _has_table_select(bind, relation):
            raise RuntimeError(
                "zf07 predecessor lacks required app_security_owner SELECT: "
                f"{relation}"
            )

    for column in (
        "branch_id",
        "org_id",
        "search_visibility_version",
        "search_provider_ack_version",
        "search_provider_reconciled_at",
    ):
        if not _has_column_select(bind, "public.org_branch_state", column):
            raise RuntimeError(
                "zf07 predecessor lacks required app_security_owner "
                f"org_branch_state.{column} SELECT"
            )


def _install_snapshots() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")

    op.execute(
        r"""
        CREATE FUNCTION app_secure.search_operational_snapshot()
        RETURNS TABLE(
            pending_count bigint,
            processing_count bigint,
            dead_letter_count bigint,
            reconciliation_candidate_count bigint,
            oldest_actionable_age_seconds double precision
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public
        SET row_security=on
        AS $function$
            WITH search_events AS (
                SELECT o.status, o.created_at
                FROM public.branch_outbox_events AS o
                WHERE o.event_type IN (
                    'branch.search_index',
                    'branch.search_deindex'
                )
            ),
            durable_work AS (
                SELECT
                    count(*) FILTER (WHERE status='pending') AS pending_count,
                    count(*) FILTER (WHERE status='processing') AS processing_count,
                    count(*) FILTER (WHERE status='dead_lettered') AS dead_letter_count,
                    min(created_at) FILTER (
                        WHERE status IN (
                            'pending',
                            'processing',
                            'dead_lettered'
                        )
                    ) AS oldest_created_at
                FROM search_events
            ),
            reconciliation AS (
                SELECT count(*)::bigint AS candidate_count
                FROM public.org_branch_state AS s
                WHERE (
                    s.search_provider_ack_version
                        IS DISTINCT FROM s.search_visibility_version
                    OR s.search_provider_reconciled_at IS NULL
                    OR s.search_provider_reconciled_at
                        < pg_catalog.clock_timestamp() - INTERVAL '24 hours'
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM public.branch_outbox_events AS existing
                    WHERE existing.branch_id = s.branch_id
                      AND existing.tenant_id = s.org_id
                      AND existing.event_type IN (
                          'branch.search_index',
                          'branch.search_deindex'
                      )
                      AND existing.status IN ('pending','processing')
                )
            )
            SELECT
                d.pending_count,
                d.processing_count,
                d.dead_letter_count,
                r.candidate_count,
                CASE
                    WHEN d.oldest_created_at IS NULL THEN 0::double precision
                    ELSE greatest(
                        0::double precision,
                        EXTRACT(
                            EPOCH FROM (
                                pg_catalog.clock_timestamp()
                                - d.oldest_created_at
                            )
                        )::double precision
                    )
                END
            FROM durable_work AS d
            CROSS JOIN reconciliation AS r
        $function$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION app_secure.refund_execution_operational_snapshot()
        RETURNS TABLE(
            pending_count bigint,
            processing_count bigint,
            retry_pending_count bigint,
            provider_accepted_count bigint,
            reconciliation_pending_count bigint,
            dead_letter_count bigint,
            oldest_unresolved_age_seconds double precision
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $function$
            SELECT
                count(*) FILTER (WHERE c.status='pending'),
                count(*) FILTER (WHERE c.status='processing'),
                count(*) FILTER (WHERE c.status='retry_pending'),
                count(*) FILTER (WHERE c.status='provider_accepted'),
                count(*) FILTER (WHERE c.status='reconciliation_pending'),
                count(*) FILTER (WHERE c.status='dead_lettered'),
                COALESCE(
                    EXTRACT(
                        EPOCH FROM (
                            pg_catalog.clock_timestamp()
                            - min(c.materialized_at) FILTER (
                                WHERE c.status IN (
                                    'pending',
                                    'processing',
                                    'retry_pending',
                                    'provider_accepted',
                                    'reconciliation_pending',
                                    'dead_lettered'
                                )
                            )
                        )
                    ),
                    0
                )::double precision
            FROM finance.refund_execution_commands AS c
            JOIN finance.refunds AS r ON r.id = c.refund_id
            WHERE r.status IN ('requested','approved','processing')
        $function$
        """
    )

    for signature in (_SEARCH_SNAPSHOT, _REFUND_SNAPSHOT):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {signature} TO {_MAINTENANCE}"
        )

    op.execute("RESET ROLE")


def _post_install_proof(bind) -> None:
    for function_name, signature in (
        (_SEARCH_SNAPSHOT_NAME, _SEARCH_SNAPSHOT),
        (_REFUND_SNAPSHOT_NAME, _REFUND_SNAPSHOT),
    ):
        row = bind.execute(
            sa.text(
                """
                SELECT p.oid, owner.rolname, p.prosecdef,
                       p.provolatile::text, p.proconfig
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = p.pronamespace
                JOIN pg_catalog.pg_roles AS owner
                  ON owner.oid = p.proowner
                WHERE n.nspname = :schema_name
                  AND p.proname = :function_name
                  AND p.pronargs = 0
                """
            ),
            {
                "schema_name": _APP_SECURE_SCHEMA,
                "function_name": function_name,
            },
        ).one_or_none()
        if row is None:
            raise RuntimeError(
                f"zf07 P4E snapshot missing after install: {signature}"
            )
        function_oid, owner, security_definer, volatility, config = row
        if owner != _SECURITY_OWNER or not security_definer:
            raise RuntimeError(
                f"zf07 P4E snapshot owner/security drift: {signature}"
            )
        if volatility != "s":
            raise RuntimeError(
                f"zf07 P4E snapshot must remain STABLE: {signature}"
            )
        config_values = set(config or ())
        if "row_security=on" not in config_values:
            raise RuntimeError(
                f"zf07 P4E snapshot lost row_security=on: {signature}"
            )
        if not any(
            value.startswith("search_path=") for value in config_values
        ):
            raise RuntimeError(
                f"zf07 P4E snapshot lost explicit search_path: {signature}"
            )

        if not bool(
            bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.has_function_privilege(
                        :role, CAST(:function_oid AS oid), 'EXECUTE'
                    )
                    """
                ),
                {
                    "role": _MAINTENANCE,
                    "function_oid": function_oid,
                },
            ).scalar_one()
        ):
            raise RuntimeError(
                f"zf07 maintenance EXECUTE missing: {signature}"
            )

        public_execute = bool(
            bind.execute(
                sa.text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_proc AS p
                        CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE p.oid = CAST(:function_oid AS oid)
                          AND acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    )
                    """
                ),
                {"function_oid": function_oid},
            ).scalar_one()
        )
        if public_execute:
            raise RuntimeError(
                f"zf07 unexpected PUBLIC EXECUTE: {signature}"
            )

        for blocked_role in (
            "app_runtime",
            "auth_runtime",
            "worker_runtime",
            "finance_config_runtime",
        ):
            if bool(
                bind.execute(
                    sa.text(
                        """
                        SELECT pg_catalog.has_function_privilege(
                            :role, CAST(:function_oid AS oid), 'EXECUTE'
                        )
                        """
                    ),
                    {
                        "role": blocked_role,
                        "function_oid": function_oid,
                    },
                ).scalar_one()
            ):
                raise RuntimeError(
                    "zf07 unexpected snapshot EXECUTE for "
                    f"{blocked_role}: {signature}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_upgrade_preconditions(bind)
    _install_snapshots()
    _post_install_proof(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)

    for function_name, signature in (
        (_SEARCH_SNAPSHOT_NAME, _SEARCH_SNAPSHOT),
        (_REFUND_SNAPSHOT_NAME, _REFUND_SNAPSHOT),
    ):
        if _function_oid(bind, function_name) is None:
            raise RuntimeError(
                f"zf07 downgrade refuses missing owned snapshot: {signature}"
            )

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION app_secure.refund_execution_operational_snapshot()"
    )
    op.execute("DROP FUNCTION app_secure.search_operational_snapshot()")
    op.execute("RESET ROLE")
