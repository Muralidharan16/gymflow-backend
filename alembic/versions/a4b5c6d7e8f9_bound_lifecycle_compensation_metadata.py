"""Bound lifecycle compensation metadata updates to the leased worker.

Revision ID: a4b5c6d7e8f9
Revises: 93a4b5c6d7e8
Create Date: 2026-08-11

A dead-letter compensation is a real canonical status change. The durable
worker already owns a live-lease-bound, column-scoped UPDATE surface for the
status and saga-control columns, but it could not persist the corresponding
status timestamp/reason/source metadata. Leaving those fields on the failed
Transaction-A transition makes the current state disagree with immutable
history and gives watchdog/reconciliation consumers a stale status age.

This revision adds only the three metadata columns needed to describe the
compensation transition. It does not grant table-wide UPDATE, change RLS, add a
role edge, or broaden the worker outside its existing live saga lease policy.
Downgrade removes exactly this revision's three-column delta.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a4b5c6d7e8f9"
down_revision = "93a4b5c6d7e8"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_WORKER = "worker_runtime"
_RELATION = "public.org_branch_state"
_POLICY = "lifecycle_worker_state_update"

_PREDECESSOR_COLUMNS = {
    "status",
    "is_operational",
    "lifecycle_transition_in_progress",
    "saga_last_checkpoint",
    "saga_compensation_strategy",
}
_METADATA_COLUMNS = {
    "status_changed_at",
    "status_reason",
    "transition_source",
}
_FORWARD_COLUMNS = _PREDECESSOR_COLUMNS | _METADATA_COLUMNS


def _require_reduced_identities(bind) -> None:
    current = bind.execute(
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
    if (
        current["session_name"] != _MIGRATION_OWNER
        or current["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("a4b5 lifecycle compensation migration requires migration_owner")
    if any(
        bool(current[key])
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
            WHERE rolname = :worker
            """
        ),
        {"worker": _WORKER},
    ).one_or_none()
    if worker is None or any(bool(value) for value in worker):
        raise RuntimeError("worker_runtime violates NOLOGIN/NOINHERIT/NOBYPASSRLS")
    if bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"),
        {"member": _MIGRATION_OWNER, "role": _WORKER},
    ).scalar_one():
        raise RuntimeError("migration_owner must not be a worker_runtime member")


def _worker_update_columns(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'org_branch_state'
                  AND grantee = :worker
                  AND privilege_type = 'UPDATE'
                ORDER BY column_name
                """
            ),
            {"worker": _WORKER},
        ).scalars().all()
    )


def _require_lease_policy(bind) -> None:
    relation = bind.execute(
        sa.text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _RELATION},
    ).one_or_none()
    if relation is None or (bool(relation[0]), bool(relation[1])) != (True, True):
        raise RuntimeError("org_branch_state must retain ENABLE + FORCE RLS")

    policy = bind.execute(
        sa.text(
            """
            SELECT policy_data.polcmd::text AS command,
                   policy_data.polroles = ARRAY[
                       (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :worker)
                   ] AS worker_only
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname = :policy
            """
        ),
        {"worker": _WORKER, "relation": _RELATION, "policy": _POLICY},
    ).mappings().one_or_none()
    if policy is None or policy["command"] != "w" or not bool(policy["worker_only"]):
        raise RuntimeError("lifecycle worker state UPDATE policy drifted")


def _require_exact_columns(bind, expected: set[str], phase: str) -> None:
    observed = _worker_update_columns(bind)
    if observed != expected:
        raise RuntimeError(
            f"a4b5 {phase} worker UPDATE drift: "
            f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
        )
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege(:worker, :relation, 'UPDATE')"
        ),
        {"worker": _WORKER, "relation": _RELATION},
    ).scalar_one():
        raise RuntimeError("worker_runtime must not receive table-wide branch-state UPDATE")


def upgrade() -> None:
    bind = op.get_bind()
    _require_reduced_identities(bind)
    _require_lease_policy(bind)
    _require_exact_columns(bind, _PREDECESSOR_COLUMNS, "predecessor")

    op.execute(
        "GRANT UPDATE (status_changed_at, status_reason, transition_source) "
        "ON TABLE public.org_branch_state TO worker_runtime"
    )

    _require_exact_columns(bind, _FORWARD_COLUMNS, "forward")


def downgrade() -> None:
    bind = op.get_bind()
    _require_reduced_identities(bind)
    _require_lease_policy(bind)
    _require_exact_columns(bind, _FORWARD_COLUMNS, "downgrade-entry")

    op.execute(
        "REVOKE UPDATE (status_changed_at, status_reason, transition_source) "
        "ON TABLE public.org_branch_state FROM worker_runtime"
    )

    _require_exact_columns(bind, _PREDECESSOR_COLUMNS, "downgrade-restored")
