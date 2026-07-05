"""add reconciliation claim lifecycle

Revision ID: 0d4e5f6a7b8c
Revises: 014167728f4a
Create Date: 2026-06-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "0d4e5f6a7b8c"
down_revision: Union[str, Sequence[str], None] = "014167728f4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RUN_CONSTRAINTS = (
    (
        "chk_platform_reconciliation_runs_claim_state",
        "claim_state IN ('idle', 'processing')",
    ),
    (
        "chk_platform_reconciliation_runs_attempt_count",
        "attempt_count >= 0",
    ),
    (
        "chk_platform_reconciliation_runs_claim_timestamps_paired",
        """
        (claimed_at IS NULL AND claim_expires_at IS NULL)
        OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)
        """,
    ),
    (
        "chk_platform_reconciliation_runs_processing_claim_metadata",
        "claim_state <> 'processing' OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
    ),
    (
        "chk_platform_reconciliation_runs_idle_claim_metadata",
        "claim_state <> 'idle' OR (claimed_at IS NULL AND claim_expires_at IS NULL)",
    ),
    (
        "chk_platform_reconciliation_runs_positive_lease",
        "claim_expires_at IS NULL OR claim_expires_at > claimed_at",
    ),
    (
        "chk_platform_reconciliation_runs_terminal_idle",
        "status NOT IN ('succeeded', 'failed', 'canceled') OR claim_state = 'idle'",
    ),
    (
        "chk_platform_reconciliation_runs_error_code_safe",
        "last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_]+$'",
    ),
)

ITEM_CONSTRAINTS = (
    (
        "chk_platform_reconciliation_items_claim_state",
        "claim_state IN ('idle', 'processing')",
    ),
    (
        "chk_platform_reconciliation_items_attempt_count",
        "attempt_count >= 0",
    ),
    (
        "chk_platform_reconciliation_items_claim_timestamps_paired",
        """
        (claimed_at IS NULL AND claim_expires_at IS NULL)
        OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)
        """,
    ),
    (
        "chk_platform_reconciliation_items_processing_claim_metadata",
        "claim_state <> 'processing' OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL)",
    ),
    (
        "chk_platform_reconciliation_items_idle_claim_metadata",
        "claim_state <> 'idle' OR (claimed_at IS NULL AND claim_expires_at IS NULL)",
    ),
    (
        "chk_platform_reconciliation_items_positive_lease",
        "claim_expires_at IS NULL OR claim_expires_at > claimed_at",
    ),
    (
        "chk_platform_reconciliation_items_terminal_idle",
        "resolution_status NOT IN ('resolved', 'ignored', 'failed') OR claim_state = 'idle'",
    ),
    (
        "chk_platform_reconciliation_items_error_code_safe",
        "last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_]+$'",
    ),
)


def _add_constraint(table_name: str, name: str, expression: str) -> None:
    op.execute(f"ALTER TABLE public.{table_name} ADD CONSTRAINT {name} CHECK ({expression});")


def _drop_constraint(table_name: str, name: str) -> None:
    op.execute(f"ALTER TABLE public.{table_name} DROP CONSTRAINT IF EXISTS {name};")


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.platform_reconciliation_runs
            ADD COLUMN claim_state TEXT NOT NULL DEFAULT 'idle',
            ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN claimed_at TIMESTAMPTZ NULL,
            ADD COLUMN claim_expires_at TIMESTAMPTZ NULL,
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            ADD COLUMN last_error_code VARCHAR(80) NULL,
            ADD COLUMN last_error_at TIMESTAMPTZ NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE public.platform_reconciliation_items
            ADD COLUMN claim_state TEXT NOT NULL DEFAULT 'idle',
            ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN claimed_at TIMESTAMPTZ NULL,
            ADD COLUMN claim_expires_at TIMESTAMPTZ NULL,
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            ADD COLUMN last_error_code VARCHAR(80) NULL,
            ADD COLUMN last_error_at TIMESTAMPTZ NULL;
        """
    )

    for name, expression in RUN_CONSTRAINTS:
        _add_constraint("platform_reconciliation_runs", name, expression)
    for name, expression in ITEM_CONSTRAINTS:
        _add_constraint("platform_reconciliation_items", name, expression)

    op.execute(
        """
        CREATE INDEX ix_platform_reconciliation_runs_claim_recovery
        ON public.platform_reconciliation_runs (status, claim_state, claim_expires_at)
        WHERE status = 'running';
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_reconciliation_items_claimable
        ON public.platform_reconciliation_items (reconciliation_run_id, created_at)
        WHERE resolution_status = 'open' AND claim_state = 'idle';
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_reconciliation_items_stale_processing
        ON public.platform_reconciliation_items (claim_expires_at)
        WHERE resolution_status = 'open' AND claim_state = 'processing';
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_reconciliation_items_run_resolution
        ON public.platform_reconciliation_items (reconciliation_run_id, resolution_status);
        """
    )

    for table_name in ("platform_reconciliation_runs", "platform_reconciliation_items"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_touch_updated_at
            BEFORE UPDATE ON public.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.platform_billing_touch_updated_at();
            """
        )


def downgrade() -> None:
    for table_name in ("platform_reconciliation_runs", "platform_reconciliation_items"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON public.{table_name};")

    for index_name in (
        "ix_platform_reconciliation_items_run_resolution",
        "ix_platform_reconciliation_items_stale_processing",
        "ix_platform_reconciliation_items_claimable",
        "ix_platform_reconciliation_runs_claim_recovery",
    ):
        op.execute(f"DROP INDEX IF EXISTS public.{index_name};")

    for name, _expression in ITEM_CONSTRAINTS:
        _drop_constraint("platform_reconciliation_items", name)
    for name, _expression in RUN_CONSTRAINTS:
        _drop_constraint("platform_reconciliation_runs", name)

    for table_name in ("platform_reconciliation_items", "platform_reconciliation_runs"):
        op.execute(
            f"""
            ALTER TABLE public.{table_name}
                DROP COLUMN IF EXISTS last_error_at,
                DROP COLUMN IF EXISTS last_error_code,
                DROP COLUMN IF EXISTS updated_at,
                DROP COLUMN IF EXISTS claim_expires_at,
                DROP COLUMN IF EXISTS claimed_at,
                DROP COLUMN IF EXISTS attempt_count,
                DROP COLUMN IF EXISTS claim_state;
            """
        )
