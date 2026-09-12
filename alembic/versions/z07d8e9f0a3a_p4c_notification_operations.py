"""Expose a PII-free maintenance snapshot for P4C notification operations.

Revision ID: z07d8e9f0a3a
Revises: y07d8e9f0a39
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "z07d8e9f0a3a"
down_revision = "y07d8e9f0a39"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SNAPSHOT = "app_secure.notification_operational_snapshot()"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(row) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("z07 P4C migration requires migration_owner")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        r"""
        CREATE FUNCTION app_secure.notification_operational_snapshot()
        RETURNS TABLE(
            pending_count bigint,
            provider_accepted_count bigint,
            dead_letter_count bigint,
            oldest_pending_age_seconds double precision
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public SET row_security=on
        AS $function$
            SELECT
                count(*) FILTER (WHERE c.status IN ('pending','retry_pending','processing')),
                count(*) FILTER (WHERE c.status='provider_accepted'),
                count(*) FILTER (WHERE c.status='dead_lettered'),
                COALESCE(
                    EXTRACT(EPOCH FROM (
                        pg_catalog.clock_timestamp()-min(c.created_at) FILTER (
                            WHERE c.status IN ('pending','retry_pending','processing','provider_accepted')
                        )
                    )),0
                )::double precision
            FROM public.notification_commands c
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SNAPSHOT} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SNAPSHOT} TO lifecycle_maintenance_runtime")
    op.execute("RESET ROLE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION IF EXISTS {_SNAPSHOT}")
    op.execute("RESET ROLE")
