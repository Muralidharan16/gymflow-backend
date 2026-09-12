"""Align branch lifecycle checkpoints with the durable worker state machine.

Revision ID: 93a4b5c6d7e8
Revises: 8293a4b5c6d7
Create Date: 2026-08-11

The original lifecycle control-plane constraint predates the durable outbox
worker. It admits checkpoint names that imply completed external side effects
(``refunds_completed``, ``notifications_sent``) while rejecting the durable
worker's actual database checkpoints (``transaction_b_started``,
``bookings_processed``, ``refunds_queued``, ``notifications_queued``).

This revision changes only that CHECK constraint. It does not change RLS, table
ACLs, role membership, or worker capabilities. Upgrade refuses any non-NULL
legacy checkpoint because an in-flight pre-durable saga may represent external
side effects that cannot be inferred safely. Downgrade likewise refuses any
non-NULL durable checkpoint because none of the new checkpoint names has an
honest predecessor equivalent. Operators must resolve the in-flight saga first;
we never relabel work as completed merely to make a migration pass.
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "93a4b5c6d7e8"
down_revision = "8293a4b5c6d7"
branch_labels = None
depends_on = None

_TABLE = "public.org_branch_state"
_CONSTRAINT = "chk_saga_last_checkpoint"
_MIGRATION_OWNER = "migration_owner"

_PREDECESSOR_CHECKPOINTS = frozenset(
    {
        "search_deindexed",
        "bookings_cancelled",
        "refunds_initiated",
        "refunds_completed",
        "notifications_sent",
        "compensation_initiated",
        "compensation_completed",
    }
)
_DURABLE_CHECKPOINTS = frozenset(
    {
        "transaction_b_started",
        "bookings_processed",
        "refunds_queued",
        "notifications_queued",
    }
)


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
                current_user::text AS current_name,
                role_data.rolsuper,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolinherit,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if (
        row["session_name"] != _MIGRATION_OWNER
        or row["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError(
            "93a4 durable checkpoint migration requires migration_owner"
        )
    if any(
        bool(row[name])
        for name in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolinherit",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")

    relation = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(class_data.relowner)::text AS owner_name,
                class_data.relrowsecurity,
                class_data.relforcerowsecurity
            FROM pg_catalog.pg_class AS class_data
            WHERE class_data.oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _TABLE},
    ).mappings().one_or_none()
    if relation is None:
        raise RuntimeError("93a4 required org_branch_state relation is missing")
    if relation["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError("93a4 org_branch_state owner contract drifted")
    if not relation["relrowsecurity"] or not relation["relforcerowsecurity"]:
        raise RuntimeError("93a4 requires ENABLE+FORCE RLS on org_branch_state")


def _constraint_row(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                constraint_data.contype::text AS constraint_type,
                constraint_data.convalidated,
                constraint_data.condeferrable,
                constraint_data.condeferred,
                pg_catalog.pg_get_constraintdef(
                    constraint_data.oid, true
                )::text AS definition
            FROM pg_catalog.pg_constraint AS constraint_data
            WHERE constraint_data.conrelid = CAST(:relation AS regclass)
              AND constraint_data.conname = :constraint_name
            """
        ),
        {"relation": _TABLE, "constraint_name": _CONSTRAINT},
    ).mappings().one_or_none()


def _constraint_values(definition: str) -> frozenset[str]:
    return frozenset(
        re.findall(r"'([^']+)'(?:::[a-zA-Z0-9_\.]+)?", definition)
    )


def _require_constraint(bind, expected: frozenset[str], label: str) -> None:
    row = _constraint_row(bind)
    if row is None:
        raise RuntimeError(f"93a4 {label} checkpoint constraint is missing")
    if (
        row["constraint_type"] != "c"
        or not row["convalidated"]
        or row["condeferrable"]
        or row["condeferred"]
    ):
        raise RuntimeError(f"93a4 {label} checkpoint constraint flags drifted")

    definition = str(row["definition"])
    normalized = " ".join(definition.lower().split())
    if (
        "saga_last_checkpoint is null" not in normalized
        or "saga_last_checkpoint = any" not in normalized
        or "array[" not in normalized
    ):
        raise RuntimeError(
            f"93a4 {label} checkpoint constraint shape drifted: {definition}"
        )
    observed = _constraint_values(definition)
    if observed != expected:
        raise RuntimeError(
            f"93a4 {label} checkpoint vocabulary drifted: "
            f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
        )


def _require_no_inflight_checkpoint(bind, phase: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                count(*)::bigint AS row_count,
                array_agg(DISTINCT saga_last_checkpoint ORDER BY saga_last_checkpoint)
                    FILTER (WHERE saga_last_checkpoint IS NOT NULL) AS checkpoints,
                array_agg(branch_id ORDER BY branch_id)
                    FILTER (WHERE saga_last_checkpoint IS NOT NULL) AS branch_ids
            FROM public.org_branch_state
            WHERE saga_last_checkpoint IS NOT NULL
            """
        )
    ).mappings().one()
    count = int(row["row_count"] or 0)
    if count:
        branches = list(row["branch_ids"] or ())[:10]
        raise RuntimeError(
            "93a4 refuses checkpoint-contract transition with unresolved "
            f"{phase} saga state: count={count}, "
            f"checkpoints={list(row['checkpoints'] or ())!r}, "
            f"sample_branch_ids={branches!r}. Resolve/compensate the saga first."
        )


def _replace_constraint(values: frozenset[str]) -> None:
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))
    op.execute(
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}"
    )
    op.execute(
        f"""
        ALTER TABLE {_TABLE}
        ADD CONSTRAINT {_CONSTRAINT}
        CHECK (
            saga_last_checkpoint IS NULL
            OR saga_last_checkpoint IN ({quoted})
        )
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_constraint(bind, _PREDECESSOR_CHECKPOINTS, "predecessor")
    _require_no_inflight_checkpoint(bind, "legacy")
    _replace_constraint(_DURABLE_CHECKPOINTS)
    _require_constraint(bind, _DURABLE_CHECKPOINTS, "durable")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_constraint(bind, _DURABLE_CHECKPOINTS, "durable")
    _require_no_inflight_checkpoint(bind, "durable")
    _replace_constraint(_PREDECESSOR_CHECKPOINTS)
    _require_constraint(bind, _PREDECESSOR_CHECKPOINTS, "predecessor")
