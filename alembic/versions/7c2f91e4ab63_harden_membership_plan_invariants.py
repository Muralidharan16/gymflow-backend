"""Harden membership-plan monetary and tenant validity invariants.

Revision ID: 7c2f91e4ab63
Revises: f9a0b1c2d3e4
Create Date: 2026-08-12

The API already treats a branch as belonging to the authenticated tenant and
requires a forward validity window, but those invariants were not authoritative
at the database boundary. This append-only revision adds validated constraints
without rewriting data, changing RLS, or widening any runtime privilege.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "7c2f91e4ab63"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_VALIDITY_CONSTRAINT = "ck_membership_plans_valid_window"
_BRANCH_TENANT_CONSTRAINT = "fk_membership_plans_branch_tenant"
_BRANCH_PAIR_CONSTRAINT = "uq_org_branch_pair"


def _bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError(
            "7c2f membership-plan hardening requires online catalog access"
        )
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable")
    return bind


def _identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   role_data.rolsuper,
                   role_data.rolinherit,
                   role_data.rolcreatedb,
                   role_data.rolcreaterole,
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
            "7c2f requires session_user=current_user=migration_owner"
        )
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
        raise RuntimeError(
            "migration_owner violates the reduced migration contract"
        )


def _constraint(bind, relation: str, name: str):
    return bind.execute(
        sa.text(
            """
            SELECT constraint_data.contype::text AS constraint_type,
                   constraint_data.convalidated,
                   pg_catalog.pg_get_constraintdef(
                       constraint_data.oid,
                       true
                   )::text AS definition
            FROM pg_catalog.pg_constraint AS constraint_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = constraint_data.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = :relation_name
              AND constraint_data.conname = :constraint_name
            """
        ),
        {
            "relation_name": relation,
            "constraint_name": name,
        },
    ).mappings().one_or_none()


def _normalized(value: str) -> str:
    return " ".join(value.lower().split())


def _require_branch_pair_key(bind) -> None:
    row = _constraint(bind, "org_branches", _BRANCH_PAIR_CONSTRAINT)
    if row is None:
        raise RuntimeError(
            "org_branches tenant-pair unique constraint is missing"
        )
    definition = _normalized(str(row["definition"]))
    if (
        row["constraint_type"] != "u"
        or not bool(row["convalidated"])
        or definition != "unique (id, org_id)"
    ):
        raise RuntimeError(
            "org_branches tenant-pair unique constraint drift: "
            f"{dict(row)!r}"
        )


def _require_absent(bind) -> None:
    for name in (_VALIDITY_CONSTRAINT, _BRANCH_TENANT_CONSTRAINT):
        if _constraint(bind, "membership_plans", name) is not None:
            raise RuntimeError(
                f"membership-plan hardening constraint already exists: {name}"
            )


def _verify_forward(bind) -> None:
    validity = _constraint(
        bind,
        "membership_plans",
        _VALIDITY_CONSTRAINT,
    )
    if validity is None:
        raise RuntimeError("membership-plan validity constraint is missing")
    validity_definition = _normalized(str(validity["definition"]))
    if (
        validity["constraint_type"] != "c"
        or not bool(validity["convalidated"])
        or "valid_from is null" not in validity_definition
        or "valid_until is null" not in validity_definition
        or "valid_until > valid_from" not in validity_definition
    ):
        raise RuntimeError(
            "membership-plan validity constraint drift: "
            f"{dict(validity)!r}"
        )

    branch_tenant = _constraint(
        bind,
        "membership_plans",
        _BRANCH_TENANT_CONSTRAINT,
    )
    if branch_tenant is None:
        raise RuntimeError(
            "membership-plan tenant/branch constraint is missing"
        )
    branch_definition = _normalized(str(branch_tenant["definition"]))
    for token in (
        "foreign key (branch_id, org_id)",
        "references org_branches(id, org_id)",
        "on delete cascade",
    ):
        if token not in branch_definition:
            raise RuntimeError(
                "membership-plan tenant/branch definition drift: "
                f"{branch_definition!r}"
            )
    if (
        branch_tenant["constraint_type"] != "f"
        or not bool(branch_tenant["convalidated"])
    ):
        raise RuntimeError(
            "membership-plan tenant/branch constraint is not a validated FK"
        )


def upgrade() -> None:
    bind = _bind()
    _identity(bind)
    _require_branch_pair_key(bind)
    _require_absent(bind)

    # NOT VALID keeps the initial DDL lock short. VALIDATE scans existing rows
    # without rewriting them and fails the transaction if historical data does
    # not satisfy the new invariant.
    op.execute(
        """
        ALTER TABLE public.membership_plans
        ADD CONSTRAINT ck_membership_plans_valid_window
        CHECK (
            valid_from IS NULL
            OR valid_until IS NULL
            OR valid_until > valid_from
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE public.membership_plans
        VALIDATE CONSTRAINT ck_membership_plans_valid_window
        """
    )

    op.execute(
        """
        ALTER TABLE public.membership_plans
        ADD CONSTRAINT fk_membership_plans_branch_tenant
        FOREIGN KEY (branch_id, org_id)
        REFERENCES public.org_branches (id, org_id)
        ON DELETE CASCADE
        NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE public.membership_plans
        VALIDATE CONSTRAINT fk_membership_plans_branch_tenant
        """
    )

    _verify_forward(bind)


def downgrade() -> None:
    bind = _bind()
    _identity(bind)
    _require_branch_pair_key(bind)
    _verify_forward(bind)

    op.execute(
        """
        ALTER TABLE public.membership_plans
        DROP CONSTRAINT fk_membership_plans_branch_tenant RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE public.membership_plans
        DROP CONSTRAINT ck_membership_plans_valid_window RESTRICT
        """
    )

    _require_absent(bind)
