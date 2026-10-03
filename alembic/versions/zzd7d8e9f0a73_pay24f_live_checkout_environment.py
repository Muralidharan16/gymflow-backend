"""PAY-24-F1 admit live environment for checkout provider operations only.

Revision ID: zzd7d8e9f0a73
Revises: zzc7d8e9f0a72
Create Date: 2026-10-03

No webhook, refund, capture, settlement, payment-application or entitlement
authority is widened here. PAY-24-A/B admission remains mandatory before any
live provider order call.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zzd7d8e9f0a73"
down_revision = "zzc7d8e9f0a72"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RESERVE_NAME = "reserve_finance_provider_operation"
_RESERVE_ARGTYPES = "uuid, text, text, text, text, text"


def _reserve_definition(bind) -> str | None:
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = p.pronamespace
            WHERE n.nspname = 'app_secure'
              AND p.proname = :function_name
              AND p.prokind = 'f'
              AND pg_catalog.oidvectortypes(p.proargtypes) = :argtypes
            """
        ),
        {
            "function_name": _RESERVE_NAME,
            "argtypes": _RESERVE_ARGTYPES,
        },
    ).scalar_one_or_none()


def _preflight(bind) -> None:
    identity = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-24-F migration requires migration_owner")

    head = bind.execute(
        sa.text("SELECT version_num::text FROM alembic_version")
    ).scalar_one()
    if head != down_revision:
        raise RuntimeError(
            f"PAY-24-F predecessor drift: expected {down_revision}, found {head}"
        )
    live_rows = bind.execute(
        sa.text(
            "SELECT count(*) FROM finance.provider_operations "
            "WHERE environment='live'"
        )
    ).scalar_one()
    if int(live_rows) != 0:
        raise RuntimeError("PAY-24-F unexpected predecessor live operations")

    definition = _reserve_definition(bind)
    if not definition:
        raise RuntimeError("PAY-24-F predecessor reserve function missing")
    if definition.count(
        "p_environment NOT IN ('sandbox','test')"
    ) != 1:
        raise RuntimeError("PAY-24-F predecessor environment gate drift")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _preflight(bind)

    definition = _reserve_definition(bind)
    if definition is None:
        raise RuntimeError("PAY-24-F reserve function disappeared")

    op.execute(
        "ALTER TABLE finance.provider_operations "
        "DROP CONSTRAINT chk_pay8_provider_environment"
    )
    op.execute(
        """
        ALTER TABLE finance.provider_operations
        ADD CONSTRAINT chk_pay8_provider_environment
        CHECK(environment IN ('sandbox','test','live')) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE finance.provider_operations "
        "VALIDATE CONSTRAINT chk_pay8_provider_environment"
    )

    successor = definition.replace(
        "p_environment NOT IN ('sandbox','test')",
        "p_environment NOT IN ('sandbox','test','live')",
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(successor)
    finally:
        op.execute("RESET ROLE")

    check = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(c.oid,true)
            FROM pg_catalog.pg_constraint c
            WHERE c.conrelid='finance.provider_operations'::regclass
              AND c.conname='chk_pay8_provider_environment'
            """
        )
    ).scalar_one()
    if "'live'::" not in str(check) and "'live'" not in str(check):
        raise RuntimeError("PAY-24-F live provider-operation constraint missing")

    current = _reserve_definition(bind)
    if current is None:
        raise RuntimeError("PAY-24-F live reserve function disappeared")
    if "p_environment NOT IN ('sandbox','test','live')" not in current:
        raise RuntimeError("PAY-24-F live reserve capability missing")
    if "record_provider_webhook" in successor:
        raise RuntimeError("PAY-24-F checkout migration touched webhook authority")


def downgrade() -> None:
    bind = op.get_bind()
    live_rows = bind.execute(
        sa.text(
            "SELECT count(*) FROM finance.provider_operations "
            "WHERE environment='live'"
        )
    ).scalar_one()
    if int(live_rows) != 0:
        raise RuntimeError(
            "PAY-24-F downgrade refused while live provider operations exist"
        )

    current = _reserve_definition(bind)
    if current is None:
        raise RuntimeError("PAY-24-F live reserve function disappeared")
    if current.count(
        "p_environment NOT IN ('sandbox','test','live')"
    ) != 1:
        raise RuntimeError("PAY-24-F live reserve function drift")

    predecessor = current.replace(
        "p_environment NOT IN ('sandbox','test','live')",
        "p_environment NOT IN ('sandbox','test')",
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(predecessor)
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "ALTER TABLE finance.provider_operations "
        "DROP CONSTRAINT chk_pay8_provider_environment"
    )
    op.execute(
        """
        ALTER TABLE finance.provider_operations
        ADD CONSTRAINT chk_pay8_provider_environment
        CHECK(environment IN ('sandbox','test')) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE finance.provider_operations "
        "VALIDATE CONSTRAINT chk_pay8_provider_environment"
    )
