"""RBAC Hardening Phase 11 - Cryptographic Key Registry

Revision ID: 970059a0665d
Revises: 45df3b75ed74
Create Date: 2026-05-23 16:17:58.085724

The audit_key_registry relation is predecessor-owned by
0023_rbac_p2_ref_tables. This revision only establishes the
branch_audit_log.hash_key_version foreign-key relationship.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '970059a0665d'
down_revision: Union[str, Sequence[str], None] = '45df3b75ed74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FK_NAME = "fk_branch_audit_log_hash_key"


def _bind():
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("970059a0665d requires an online database connection")
    return bind


def _preflight_registry_contract(bind) -> None:
    """Require the predecessor-owned registry contract without adopting it."""
    relation = bind.execute(
        sa.text(
            """
            SELECT
                c.relkind::text AS relkind,
                pg_catalog.pg_get_userbyid(c.relowner)::text AS owner_name
            FROM pg_catalog.pg_class AS c
            WHERE c.oid = pg_catalog.to_regclass('public.audit_key_registry')
            """
        )
    ).mappings().first()
    if relation is None or relation["relkind"] not in ("r", "p"):
        raise RuntimeError(
            "970059a0665d requires predecessor-owned public.audit_key_registry"
        )

    required_columns = {
        "key_version": "smallint",
        "kms_key_alias": "character varying",
        "algorithm": "character varying",
        "digest_algorithm": "character varying",
        "signature_algorithm": "character varying",
        "rotation_date": "timestamp with time zone",
        "retirement_date": "timestamp with time zone",
        "is_active": "boolean",
    }
    observed_columns = {
        row["column_name"]: row["data_type"]
        for row in bind.execute(
            sa.text(
                """
                SELECT column_name::text, data_type::text
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'audit_key_registry'
                """
            )
        ).mappings()
    }
    missing_or_wrong = {
        name: (expected, observed_columns.get(name))
        for name, expected in required_columns.items()
        if observed_columns.get(name) != expected
    }
    if missing_or_wrong:
        raise RuntimeError(
            "970059a0665d audit_key_registry structural drift: "
            f"{missing_or_wrong!r}"
        )

    pk_columns = tuple(
        row["column_name"]
        for row in bind.execute(
            sa.text(
                """
                SELECT a.attname::text AS column_name
                FROM pg_catalog.pg_constraint AS con
                JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
                JOIN pg_catalog.pg_namespace AS nsp ON nsp.oid = rel.relnamespace
                JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord)
                  ON TRUE
                JOIN pg_catalog.pg_attribute AS a
                  ON a.attrelid = rel.oid AND a.attnum = key.attnum
                WHERE nsp.nspname = 'public'
                  AND rel.relname = 'audit_key_registry'
                  AND con.contype = 'p'
                ORDER BY key.ord
                """
            )
        ).mappings()
    )
    if pk_columns != ("key_version",):
        raise RuntimeError(
            "970059a0665d requires audit_key_registry primary key (key_version)"
        )


def _foreign_key_contract(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                con.conname::text AS constraint_name,
                pg_catalog.pg_get_constraintdef(con.oid, true)::text AS definition
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS nsp ON nsp.oid = rel.relnamespace
            WHERE nsp.nspname = 'public'
              AND rel.relname = 'branch_audit_log'
              AND con.conname = :constraint_name
              AND con.contype = 'f'
            """
        ),
        {"constraint_name": _FK_NAME},
    ).mappings().all()
    return tuple((row["constraint_name"], row["definition"]) for row in rows)


def upgrade() -> None:
    bind = _bind()
    _preflight_registry_contract(bind)

    if _foreign_key_contract(bind):
        raise RuntimeError(
            "970059a0665d refuses to adopt existing "
            f"{_FK_NAME}"
        )

    op.execute(
        """
        ALTER TABLE public.branch_audit_log
            ADD CONSTRAINT fk_branch_audit_log_hash_key
            FOREIGN KEY (hash_key_version)
            REFERENCES public.audit_key_registry(key_version)
            ON DELETE RESTRICT
        """
    )

    observed = _foreign_key_contract(bind)
    if len(observed) != 1:
        raise RuntimeError(
            "970059a0665d failed to establish exactly one canonical audit-key FK"
        )
    definition = observed[0][1].upper()
    for fragment in (
        "FOREIGN KEY (HASH_KEY_VERSION)",
        "REFERENCES AUDIT_KEY_REGISTRY(KEY_VERSION)",
        "ON DELETE RESTRICT",
    ):
        if fragment not in definition:
            raise RuntimeError(
                "970059a0665d established an unexpected FK definition: "
                f"{observed[0][1]!r}"
            )


def downgrade() -> None:
    bind = _bind()
    _preflight_registry_contract(bind)

    observed = _foreign_key_contract(bind)
    if len(observed) != 1:
        raise RuntimeError(
            "970059a0665d downgrade requires its canonical audit-key FK"
        )
    definition = observed[0][1].upper()
    for fragment in (
        "FOREIGN KEY (HASH_KEY_VERSION)",
        "REFERENCES AUDIT_KEY_REGISTRY(KEY_VERSION)",
        "ON DELETE RESTRICT",
    ):
        if fragment not in definition:
            raise RuntimeError(
                "970059a0665d refuses to drop an unexpected FK contract: "
                f"{observed[0][1]!r}"
            )

    # This revision owns only the FK. The audit_key_registry relation and all
    # key-rotation history remain predecessor-owned and must survive rollback.
    op.execute(
        "ALTER TABLE public.branch_audit_log "
        "DROP CONSTRAINT fk_branch_audit_log_hash_key RESTRICT"
    )

    if _foreign_key_contract(bind):
        raise RuntimeError(
            "970059a0665d downgrade failed to remove its audit-key FK"
        )
