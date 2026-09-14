"""P5-D: restore only the API columns required to enqueue lifecycle outbox work.

Revision ID: zj07d8e9f0a44
Revises: zi07d8e9f0a43
Create Date: 2026-09-14

The canonical lifecycle Transaction-A path persists tenant-scoped search and
lifecycle-saga commands directly in ``branch_outbox_events``. A later security
hardening chain removed the application runtime's effective INSERT authority,
which made the legal lifecycle mutation fail before P5-D could reach its
PostgreSQL disconnect boundary.

Do not restore table-wide INSERT. Grant ``app_runtime`` INSERT on only the
columns emitted by ``BranchLifecycleService``/its ORM defaults. Existing FORCE
RLS and the app-runtime-only ``p_outbox_insert`` policy remain the row authority.
Worker and PUBLIC INSERT remain denied. Downgrade removes exactly this P5-D
column grant and recreates the zi07 predecessor authority state.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "zj07d8e9f0a44"
down_revision = "zi07d8e9f0a43"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_APP = "app_runtime"
_WORKER = "worker_runtime"
_OUTBOX = "public.branch_outbox_events"
_POLICY = "p_outbox_insert"
_INSERT_COLUMNS = (
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
    "lease_fence",
)


def _identity_row(bind):
    return bind.execute(
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


def _require_identity(bind) -> None:
    row = _identity_row(bind)
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("zj07 P5-D enqueue authority migration requires migration_owner")
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
        raise RuntimeError("zj07 migration_owner violates the reduced role contract")


def _role_oid(bind, role_name: str) -> int:
    value = bind.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:role_name"),
        {"role_name": role_name},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"zj07 required role is missing: {role_name}")
    return int(value)


def _has_table_insert(bind, role_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT pg_catalog.has_table_privilege(:role_name,:relation,'INSERT')"),
            {"role_name": role_name, "relation": _OUTBOX},
        ).scalar_one()
    )


def _public_has_table_insert(bind) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT COALESCE(bool_or(acl_data.privilege_type='INSERT'),FALSE)
                FROM pg_catalog.pg_class AS relation_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl_data
                WHERE relation_data.oid=CAST(:relation AS regclass)
                  AND acl_data.grantee=0
                """
            ),
            {"relation": _OUTBOX},
        ).scalar_one()
    )


def _has_column_insert(bind, role_name: str, column_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_column_privilege("
                ":role_name,:relation,:column_name,'INSERT')"
            ),
            {"role_name": role_name, "relation": _OUTBOX, "column_name": column_name},
        ).scalar_one()
    )


def _public_has_column_insert(bind, column_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT COALESCE(bool_or(acl_data.privilege_type='INSERT'),FALSE)
                FROM pg_catalog.pg_attribute AS column_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(column_data.attacl, ARRAY[]::aclitem[])
                ) AS acl_data
                WHERE column_data.attrelid=CAST(:relation AS regclass)
                  AND column_data.attname=:column_name
                  AND column_data.attnum>0
                  AND NOT column_data.attisdropped
                  AND acl_data.grantee=0
                """
            ),
            {"relation": _OUTBOX, "column_name": column_name},
        ).scalar_one()
    )


def _require_force_rls(bind) -> None:
    row = bind.execute(
        sa.text(
            "SELECT relrowsecurity,relforcerowsecurity "
            "FROM pg_catalog.pg_class WHERE oid=CAST(:relation AS regclass)"
        ),
        {"relation": _OUTBOX},
    ).one_or_none()
    if row is None or (bool(row[0]), bool(row[1])) != (True, True):
        raise RuntimeError("zj07 requires branch_outbox_events ENABLE+FORCE RLS")


def _require_policy(bind) -> None:
    app_oid = _role_oid(bind, _APP)
    row = bind.execute(
        sa.text(
            """
            SELECT polcmd::text AS command, polpermissive, polroles
            FROM pg_catalog.pg_policy
            WHERE polrelid=CAST(:relation AS regclass) AND polname=:policy
            """
        ),
        {"relation": _OUTBOX, "policy": _POLICY},
    ).mappings().one_or_none()
    if (
        row is None
        or row["command"] != "a"
        or not bool(row["polpermissive"])
        or list(row["polroles"]) != [app_oid]
    ):
        raise RuntimeError("zj07 app-runtime outbox INSERT policy contract drifted")


def _require_predecessor(bind) -> None:
    _require_force_rls(bind)
    _require_policy(bind)
    if _has_table_insert(bind, _APP):
        raise RuntimeError("zj07 refuses predecessor table-wide app_runtime INSERT")
    if _has_table_insert(bind, _WORKER):
        raise RuntimeError("zj07 refuses predecessor worker table-wide INSERT")
    if _public_has_table_insert(bind):
        raise RuntimeError("zj07 refuses predecessor PUBLIC table-wide INSERT")
    collisions = [
        column_name for column_name in _INSERT_COLUMNS
        if _has_column_insert(bind, _APP, column_name)
    ]
    if collisions:
        raise RuntimeError(
            "zj07 refuses ambiguous predecessor app_runtime column INSERT: "
            f"{collisions!r}"
        )


def _verify_forward(bind) -> None:
    _require_force_rls(bind)
    _require_policy(bind)
    if _has_table_insert(bind, _APP):
        raise RuntimeError("zj07 accidentally broadened app_runtime to table-wide INSERT")
    missing = [
        column_name for column_name in _INSERT_COLUMNS
        if not _has_column_insert(bind, _APP, column_name)
    ]
    if missing:
        raise RuntimeError(f"zj07 app_runtime lifecycle enqueue columns missing: {missing!r}")
    if _has_table_insert(bind, _WORKER):
        raise RuntimeError("zj07 leaked table-wide outbox INSERT to worker_runtime")
    worker_leaked = [
        column_name for column_name in _INSERT_COLUMNS
        if _has_column_insert(bind, _WORKER, column_name)
    ]
    if worker_leaked:
        raise RuntimeError(
            f"zj07 leaked outbox INSERT columns to worker_runtime: {worker_leaked!r}"
        )
    if _public_has_table_insert(bind):
        raise RuntimeError("zj07 leaked table-wide outbox INSERT to PUBLIC")
    public_leaked = [
        column_name for column_name in _INSERT_COLUMNS
        if _public_has_column_insert(bind, column_name)
    ]
    if public_leaked:
        raise RuntimeError(f"zj07 leaked outbox INSERT columns to PUBLIC: {public_leaked!r}")


def _verify_downgrade(bind) -> None:
    _require_force_rls(bind)
    _require_policy(bind)
    if _has_table_insert(bind, _APP):
        raise RuntimeError("zj07 downgrade left table-wide app_runtime INSERT")
    leftovers = [
        column_name for column_name in _INSERT_COLUMNS
        if _has_column_insert(bind, _APP, column_name)
    ]
    if leftovers:
        raise RuntimeError(
            f"zj07 downgrade left app_runtime outbox INSERT columns: {leftovers!r}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)
    columns = ", ".join(_INSERT_COLUMNS)
    op.execute(f"GRANT INSERT ({columns}) ON TABLE {_OUTBOX} TO {_APP}")
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _verify_forward(bind)
    columns = ", ".join(_INSERT_COLUMNS)
    op.execute(f"REVOKE INSERT ({columns}) ON TABLE {_OUTBOX} FROM {_APP}")
    _verify_downgrade(bind)
