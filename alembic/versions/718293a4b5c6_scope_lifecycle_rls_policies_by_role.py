"""Scope lifecycle RLS policies to their intended database identities.

Revision ID: 718293a4b5c6
Revises: 60718293a4b5
Create Date: 2026-08-11

The lifecycle policies introduced by 708/92 were created without ``TO`` and
therefore applied to PUBLIC. After the dedicated worker boundary was added,
PostgreSQL composed those ordinary API policies into worker queries as well.
In particular, ``p_outbox_select`` resolves tenant ownership through
``org_branches`` while ``lifecycle_worker_branch_read`` resolves its lease
through ``branch_outbox_events``, creating the RLS rewrite cycle:

    org_branches -> branch_outbox_events -> org_branches

This revision changes *only* the policy role domain. It uses ``ALTER POLICY ...
TO ...`` so PostgreSQL preserves the exact predecessor ``USING`` and
``WITH CHECK`` expressions, command and permissive mode rather than asking this
revision to reconstruct security predicates owned by 708/92. Normal lifecycle
policies are scoped to ``app_runtime``; onboarding branch-state INSERT is scoped
to ``auth_runtime``. Dedicated worker/security-owner policies remain separate.
Downgrade restores the exact predecessor policy definitions by changing only the
role list back to PUBLIC.
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
                   pg_catalog.pg_get_expr(p.polqual, p.polrelid, true)::text AS using_expr,
                   pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid, true)::text AS check_expr
            FROM pg_catalog.pg_policy AS p
            WHERE p.polrelid = CAST(:relation AS regclass)
              AND p.polname = :policy_name
            """
        ),
        {"relation": relation, "policy_name": policy_name},
    ).mappings().one_or_none()


def _predicate_contract(row) -> tuple[object, ...]:
    return (
        row["command"],
        bool(row["polpermissive"]),
        row["using_expr"],
        row["check_expr"],
    )


def _capture_public_predecessor(bind) -> dict[tuple[str, str], tuple[object, ...]]:
    contracts: dict[tuple[str, str], tuple[object, ...]] = {}
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
            contracts[(relation, policy_name)] = _predicate_contract(row)
    return contracts


def _role_oid(bind, role_name: str) -> int:
    value = bind.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"718293 required role is missing: {role_name}")
    return int(value)


def _alter_policy_roles(targets: dict[tuple[str, str], str]) -> None:
    for (relation, policy_name), role_name in targets.items():
        op.execute(f"ALTER POLICY {policy_name} ON {relation} TO {role_name}")


def _target_map() -> dict[tuple[str, str], str]:
    return {
        (relation, policy_name): role_name
        for relation, policies in _POLICY_RELATIONS.items()
        for policy_name, role_name in policies.items()
    }


def _verify_scoped(
    bind,
    predecessor_contracts: dict[tuple[str, str], tuple[object, ...]],
) -> None:
    role_oids = {
        name: _role_oid(bind, name)
        for name in ("app_runtime", "auth_runtime")
    }
    for (relation, policy_name), role_name in _target_map().items():
        row = _policy_row(bind, relation, policy_name)
        if row is None:
            raise RuntimeError(
                f"718293 scoped policy disappeared: {relation}.{policy_name}"
            )
        if list(row["polroles"]) != [role_oids[role_name]]:
            raise RuntimeError(
                "718293 policy scope postcondition failed: "
                f"{relation}.{policy_name}: expected={role_name}, "
                f"observed={list(row['polroles'])!r}"
            )
        if _predicate_contract(row) != predecessor_contracts[(relation, policy_name)]:
            raise RuntimeError(
                "718293 changed lifecycle policy predicate while scoping roles: "
                f"{relation}.{policy_name}"
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


def _capture_scoped_for_downgrade(bind) -> dict[tuple[str, str], tuple[object, ...]]:
    contracts: dict[tuple[str, str], tuple[object, ...]] = {}
    role_oids = {
        name: _role_oid(bind, name)
        for name in ("app_runtime", "auth_runtime")
    }
    for (relation, policy_name), role_name in _target_map().items():
        row = _policy_row(bind, relation, policy_name)
        if row is None or list(row["polroles"]) != [role_oids[role_name]]:
            raise RuntimeError(
                f"718293 downgrade policy-role drift: {relation}.{policy_name}"
            )
        contracts[(relation, policy_name)] = _predicate_contract(row)
    return contracts


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    predecessor_contracts = _capture_public_predecessor(bind)
    _alter_policy_roles(_target_map())
    _verify_scoped(bind, predecessor_contracts)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    scoped_contracts = _capture_scoped_for_downgrade(bind)
    _alter_policy_roles({key: "PUBLIC" for key in _target_map()})

    for (relation, policy_name), expected_contract in scoped_contracts.items():
        row = _policy_row(bind, relation, policy_name)
        if row is None or list(row["polroles"]) != [0]:
            raise RuntimeError(
                f"718293 failed to restore PUBLIC scope: {relation}.{policy_name}"
            )
        if _predicate_contract(row) != expected_contract:
            raise RuntimeError(
                "718293 downgrade changed lifecycle policy predicate: "
                f"{relation}.{policy_name}"
            )
