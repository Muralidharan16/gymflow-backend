"""Enforce branch-hours outbox retry and lease state invariants.

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f
Create Date: 2026-08-11

Worker code already caps delivery attempts and clears leases on terminal states.
These are durable queue invariants and belong in PostgreSQL as well: corrupted
state must be rejected even if a future worker implementation regresses.
"""

from alembic import op
import sqlalchemy as sa


revision = "1b2c3d4e5f60"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text, current_user::text,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).one()
    if row[0] != "migration_owner" or row[1] != "migration_owner":
        raise RuntimeError("1b2c outbox-state migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    invalid = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.transactional_outbox
            WHERE delivery_attempts < 0
               OR delivery_attempts > 15
               OR (processed_at IS NOT NULL AND dead_lettered_at IS NOT NULL)
               OR ((leased_by IS NULL) IS DISTINCT FROM (leased_until IS NULL))
               OR (
                    (processed_at IS NOT NULL OR dead_lettered_at IS NOT NULL)
                    AND (leased_by IS NOT NULL OR leased_until IS NOT NULL)
               )
            """
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(
            f"1b2c refuses invalid predecessor outbox state: count={invalid}"
        )

    op.execute(
        "ALTER TABLE public.transactional_outbox "
        "DROP CONSTRAINT chk_transactional_outbox_attempts_nonnegative"
    )
    op.execute(
        """
        ALTER TABLE public.transactional_outbox
        ADD CONSTRAINT chk_transactional_outbox_attempts_range
            CHECK (delivery_attempts BETWEEN 0 AND 15),
        ADD CONSTRAINT chk_transactional_outbox_terminal_state
            CHECK (NOT (processed_at IS NOT NULL AND dead_lettered_at IS NOT NULL)),
        ADD CONSTRAINT chk_transactional_outbox_lease_pair
            CHECK ((leased_by IS NULL) = (leased_until IS NULL)),
        ADD CONSTRAINT chk_transactional_outbox_terminal_unleased
            CHECK (
                (processed_at IS NULL AND dead_lettered_at IS NULL)
                OR (leased_by IS NULL AND leased_until IS NULL)
            )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    op.execute(
        """
        ALTER TABLE public.transactional_outbox
        DROP CONSTRAINT chk_transactional_outbox_terminal_unleased,
        DROP CONSTRAINT chk_transactional_outbox_lease_pair,
        DROP CONSTRAINT chk_transactional_outbox_terminal_state,
        DROP CONSTRAINT chk_transactional_outbox_attempts_range,
        ADD CONSTRAINT chk_transactional_outbox_attempts_nonnegative
            CHECK (delivery_attempts >= 0)
        """
    )
