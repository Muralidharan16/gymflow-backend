"""Add monotonic claim fences to the two core durable outboxes.

Revision ID: zg07d8e9f0a41
Revises: zf07d8e9f0a40
Create Date: 2026-09-12

P5-W1 makes a claim generation durable independently of worker UUID and lease
time.  Reclaim increments the generation without consuming another attempt, so
an expired final attempt remains recoverable and an ABA-style stale execution
cannot complete against a later claim made with the same worker UUID.

No provider, financial execution, RLS policy or business state is added here.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zg07d8e9f0a41"
down_revision = "zf07d8e9f0a40"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_WORKER = "worker_runtime"
_TABLES = (
    ("public", "branch_outbox_events", "chk_branch_outbox_lease_fence"),
    ("public", "transactional_outbox", "chk_transactional_outbox_lease_fence"),
)
_NON_WORKER_RUNTIME_ROLES = (
    "app_runtime",
    "app_user",
    "auth_runtime",
    "finance_config_runtime",
    "lifecycle_maintenance_runtime",
)
_BRANCH_INSERT_COLUMNS = (
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
    "last_attempted_at",
    "last_error",
    "correlation_id",
    "leased_by",
    "leased_until",
)
_FENCE_INSERT_FORBIDDEN_ROLES = (
    "app_runtime",
    "app_user",
    "app_security_owner",
    "auth_runtime",
    "finance_config_runtime",
    "lifecycle_maintenance_runtime",
    "worker_runtime",
)


def _require_identity(bind) -> None:
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
        raise RuntimeError("zg07 P5-W1 migration requires migration_owner")
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
        raise RuntimeError("zg07 migration_owner violates the reduced-role contract")


def _column_metadata(bind, schema_name: str, table_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT a.attnotnull,
                   pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type,
                   pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default_expr
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
            LEFT JOIN pg_catalog.pg_attrdef AS d
              ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE n.nspname=:schema_name
              AND c.relname=:table_name
              AND a.attname='lease_fence'
              AND a.attnum>0
              AND NOT a.attisdropped
            """
        ),
        {"schema_name": schema_name, "table_name": table_name},
    ).mappings().one_or_none()


def _constraint_exists(bind, schema_name: str, table_name: str, constraint_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint AS con
                    JOIN pg_catalog.pg_class AS c ON c.oid=con.conrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
                    WHERE n.nspname=:schema_name
                      AND c.relname=:table_name
                      AND con.conname=:constraint_name
                )
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "constraint_name": constraint_name,
            },
        ).scalar_one()
    )


def _rls_state(bind, relation: str) -> tuple[bool, bool]:
    row = bind.execute(
        sa.text(
            """
            SELECT relrowsecurity,relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid=CAST(:relation AS regclass)
            """
        ),
        {"relation": relation},
    ).one()
    return bool(row[0]), bool(row[1])


def _has_table_update(bind, role_name: str, relation: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT pg_catalog.has_table_privilege(:role,:relation,'UPDATE')"),
            {"role": role_name, "relation": relation},
        ).scalar_one()
    )


def _has_table_insert(bind, role_name: str, relation: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT pg_catalog.has_table_privilege(:role,:relation,'INSERT')"),
            {"role": role_name, "relation": relation},
        ).scalar_one()
    )


def _has_column_update(bind, role_name: str, relation: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,'lease_fence','UPDATE')"
            ),
            {"role": role_name, "relation": relation},
        ).scalar_one()
    )


def _has_column_insert(bind, role_name: str, relation: str, column_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege(:role,:relation,:column,'INSERT')"
            ),
            {"role": role_name, "relation": relation, "column": column_name},
        ).scalar_one()
    )


def _assert_predecessor(bind) -> None:
    for schema_name, table_name, constraint_name in _TABLES:
        relation = f"{schema_name}.{table_name}"
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"zg07 predecessor relation is absent: {relation}")
        if _column_metadata(bind, schema_name, table_name) is not None:
            raise RuntimeError(f"zg07 lease_fence column collision: {relation}")
        if _constraint_exists(bind, schema_name, table_name, constraint_name):
            raise RuntimeError(f"zg07 lease-fence constraint collision: {relation}")
        for role_name in (_WORKER, *_NON_WORKER_RUNTIME_ROLES):
            if _has_table_update(bind, role_name, relation):
                raise RuntimeError(
                    f"zg07 refuses table-wide UPDATE authority: role={role_name} relation={relation}"
                )
    if not _has_table_insert(bind, "app_runtime", "public.branch_outbox_events"):
        raise RuntimeError("zg07 predecessor app_runtime branch outbox INSERT drift")


def _assert_installed(bind) -> None:
    for schema_name, table_name, constraint_name in _TABLES:
        relation = f"{schema_name}.{table_name}"
        metadata = _column_metadata(bind, schema_name, table_name)
        if metadata is None:
            raise RuntimeError(f"zg07 lease_fence column is absent: {relation}")
        if not metadata["attnotnull"] or metadata["data_type"] != "bigint":
            raise RuntimeError(f"zg07 lease_fence type/nullability drift: {relation}")
        if metadata["default_expr"] not in {"0", "0::bigint"}:
            raise RuntimeError(f"zg07 lease_fence default drift: {relation}")
        if not _constraint_exists(bind, schema_name, table_name, constraint_name):
            raise RuntimeError(f"zg07 lease-fence constraint is absent: {relation}")
        if not _has_column_update(bind, _WORKER, relation):
            raise RuntimeError(f"zg07 worker lacks lease_fence UPDATE: {relation}")
        for role_name in _NON_WORKER_RUNTIME_ROLES:
            if _has_column_update(bind, role_name, relation):
                raise RuntimeError(
                    f"zg07 non-worker lease_fence UPDATE leaked: role={role_name} relation={relation}"
                )
        for role_name in _FENCE_INSERT_FORBIDDEN_ROLES:
            if _has_column_insert(bind, role_name, relation, "lease_fence"):
                raise RuntimeError(
                    f"zg07 lease_fence INSERT leaked: role={role_name} relation={relation}"
                )

    branch_outbox = "public.branch_outbox_events"
    if _has_table_insert(bind, "app_runtime", branch_outbox):
        raise RuntimeError("zg07 app_runtime retained table-wide branch outbox INSERT")
    missing_columns = [
        column_name
        for column_name in _BRANCH_INSERT_COLUMNS
        if not _has_column_insert(bind, "app_runtime", branch_outbox, column_name)
    ]
    if missing_columns:
        raise RuntimeError(
            "zg07 app_runtime branch outbox INSERT column drift: "
            f"missing={missing_columns!r}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _assert_predecessor(bind)

    for schema_name, table_name, constraint_name in _TABLES:
        relation = f"{schema_name}.{table_name}"
        op.execute(
            f"""
            ALTER TABLE {relation}
                ADD COLUMN lease_fence bigint NOT NULL DEFAULT 0,
                ADD CONSTRAINT {constraint_name} CHECK (lease_fence >= 0)
            """
        )
        op.execute(
            f"GRANT UPDATE (lease_fence) ON TABLE {relation} TO {_WORKER}"
        )

    branch_columns = ",".join(_BRANCH_INSERT_COLUMNS)
    op.execute("REVOKE INSERT ON TABLE public.branch_outbox_events FROM app_runtime")
    op.execute(
        "GRANT INSERT "
        f"({branch_columns}) ON TABLE public.branch_outbox_events TO app_runtime"
    )

    _assert_installed(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _assert_installed(bind)

    relations = [
        f"{schema_name}.{table_name}"
        for schema_name, table_name, _constraint_name in _TABLES
    ]
    for relation in relations:
        if _rls_state(bind, relation) != (True, True):
            raise RuntimeError(
                f"zg07 inherited FORCE RLS state drift before downgrade: {relation}"
            )

    # migration_owner owns both tables but is deliberately subject to FORCE
    # RLS and is not a worker role. Temporarily remove only owner enforcement
    # inside this transactional migration so the destructive-downgrade guard
    # cannot mistake policy-hidden rows for an empty table. RLS remains enabled
    # for every non-owner, and FORCE is restored before the guard decision.
    for relation in relations:
        op.execute(f"ALTER TABLE {relation} NO FORCE ROW LEVEL SECURITY")
    try:
        claimed = {}
        for relation in relations:
            count = int(
                bind.execute(
                    sa.text(f"SELECT count(*) FROM {relation} WHERE lease_fence <> 0")
                ).scalar_one()
            )
            if count:
                claimed[relation] = count
    finally:
        for relation in relations:
            op.execute(f"ALTER TABLE {relation} FORCE ROW LEVEL SECURITY")

    for relation in relations:
        if _rls_state(bind, relation) != (True, True):
            raise RuntimeError(
                f"zg07 failed to restore FORCE RLS before downgrade decision: {relation}"
            )
    if claimed:
        raise RuntimeError(
            "zg07 downgrade refuses loss of durable P5 claim-generation evidence: "
            f"{claimed!r}"
        )

    branch_columns = ",".join(_BRANCH_INSERT_COLUMNS)
    op.execute(
        "REVOKE INSERT "
        f"({branch_columns}) ON TABLE public.branch_outbox_events FROM app_runtime"
    )

    for schema_name, table_name, constraint_name in reversed(_TABLES):
        relation = f"{schema_name}.{table_name}"
        op.execute(
            f"REVOKE UPDATE (lease_fence) ON TABLE {relation} FROM {_WORKER}"
        )
        op.execute(
            f"""
            ALTER TABLE {relation}
                DROP CONSTRAINT {constraint_name},
                DROP COLUMN lease_fence
            """
        )

    op.execute("GRANT INSERT ON TABLE public.branch_outbox_events TO app_runtime")

    for schema_name, table_name, _constraint_name in _TABLES:
        if _column_metadata(bind, schema_name, table_name) is not None:
            raise RuntimeError(
                f"zg07 downgrade failed to remove lease_fence: {schema_name}.{table_name}"
            )
    if not _has_table_insert(bind, "app_runtime", "public.branch_outbox_events"):
        raise RuntimeError("zg07 downgrade failed to restore branch outbox INSERT")
