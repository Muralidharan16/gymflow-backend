"""Index lifecycle outbox pending and expired-processing claim paths.

Revision ID: 4e5f60718293
Revises: 3d4e5f607182
Create Date: 2026-08-11

A durable worker must reclaim rows left in ``processing`` after a worker crash,
not only fresh ``pending`` rows. The predecessor pending-only partial index does
not support that recovery path efficiently at scale.
"""

from alembic import op
import sqlalchemy as sa


revision = "4e5f60718293"
down_revision = "3d4e5f607182"
branch_labels = None
depends_on = None


def _require_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            "SELECT session_user::text, current_user::text, rolsuper, rolinherit, "
            "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
    ).one()
    if row[0] != "migration_owner" or row[1] != "migration_owner":
        raise RuntimeError("4e5f lifecycle claim-index migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def upgrade() -> None:
    bind = op.get_bind()
    _require_owner(bind)
    op.execute("DROP INDEX public.ix_branch_outbox_ready_claim")
    op.execute(
        """
        CREATE INDEX ix_branch_outbox_claimable
        ON public.branch_outbox_events (
            status,
            process_after,
            leased_until,
            created_at,
            outbox_id
        )
        WHERE status IN ('pending', 'processing')
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_owner(bind)
    op.execute("DROP INDEX public.ix_branch_outbox_claimable")
    op.execute(
        """
        CREATE INDEX ix_branch_outbox_ready_claim
        ON public.branch_outbox_events (process_after, created_at, outbox_id)
        WHERE status = 'pending'
        """
    )
