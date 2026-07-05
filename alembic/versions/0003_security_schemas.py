"""
Alembic migration: core security schemas — encryption key registry + address payloads
======================================================================================
Creates:
  • public.encryption_key_registry — DEK lifecycle table (version, status, rotation)
  • public.organization_address_payloads_secure — AES-GCM encrypted address payloads
  • public.address_audit_ledger — append-only HMAC-chained audit log
  • public.audit_chain_heads — per-entity audit chain head tracker

Schema hardening (Master Blueprint Section 3):
  • SECURITY DEFINER functions reference pg_catalog explicitly.
  • fillfactor=80 on hot update tables to reduce page splits.
  • BRIN indexes on timestamp columns for range scan efficiency.

Revision: 0003_security_schemas
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision      = "0003_security_schemas"
down_revision = "0002_enterprise_platform"
branch_labels = None
depends_on    = None


def upgrade() -> None:

    # ── encryption_key_registry ──────────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.encryption_key_registry (
            key_version     SERIAL      PRIMARY KEY,
            tenant_id       UUID        NOT NULL,
            table_name      VARCHAR(100) NOT NULL,
            encrypted_dek   BYTEA       NOT NULL,
            key_status      VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (key_status IN ('ACTIVE','DEPRECATED','RETIRED')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            deprecated_at   TIMESTAMPTZ NULL,
            retired_at      TIMESTAMPTZ NULL
        ) WITH (fillfactor = 90)
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_key_registry_active
            ON public.encryption_key_registry (tenant_id, table_name)
            WHERE key_status = 'ACTIVE'
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_key_registry_tenant_ver
            ON public.encryption_key_registry (tenant_id, key_version)
    """)

    # ── organization_address_payloads_secure ─────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.organization_address_payloads_secure (
            id                UUID        PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            tenant_id         UUID        NOT NULL,
            org_id            UUID        NOT NULL,
            address_id        UUID        NOT NULL,
            payload_encrypted BYTEA       NOT NULL,
            key_version       INTEGER     NOT NULL
                              REFERENCES public.encryption_key_registry (key_version),
            schema_version    SMALLINT    NOT NULL DEFAULT 1,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
        ) WITH (fillfactor = 80)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_addr_payloads_tenant_org
            ON public.organization_address_payloads_secure (tenant_id, org_id)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_addr_payloads_key_version
            ON public.organization_address_payloads_secure (key_version)
            WHERE key_version > 0
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_addr_payloads_created_brin
            ON public.organization_address_payloads_secure USING BRIN (created_at)
    """)

    # ── address_audit_ledger (append-only, HMAC-chained) ─────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.address_audit_ledger (
            id              BIGSERIAL   PRIMARY KEY,
            tenant_id       UUID        NOT NULL,
            entity_id       UUID        NOT NULL,
            entity_type     VARCHAR(50) NOT NULL,
            event_type      VARCHAR(50) NOT NULL,
            event_version   SMALLINT    NOT NULL DEFAULT 1,
            changed_by      UUID        NOT NULL,
            changed_at      TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            payload_hash    CHAR(64)    NOT NULL,
            chain_hmac      CHAR(64)    NOT NULL,
            prev_chain_hmac CHAR(64)    NULL,
            ip_address      VARCHAR(45) NULL,
            trace_id        VARCHAR(128) NULL,
            metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb
        ) WITH (fillfactor = 100)
    """)

    # Prevent any updates or deletes — audit rows are immutable
    op.execute("""
        CREATE RULE address_audit_ledger_no_update AS
            ON UPDATE TO public.address_audit_ledger
            DO INSTEAD NOTHING
    """)

    op.execute("""
        CREATE RULE address_audit_ledger_no_delete AS
            ON DELETE TO public.address_audit_ledger
            DO INSTEAD NOTHING
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_ledger_entity
            ON public.address_audit_ledger (tenant_id, entity_id, changed_at)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_ledger_changed_brin
            ON public.address_audit_ledger USING BRIN (changed_at)
    """)

    # ── audit_chain_heads ────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.audit_chain_heads (
            tenant_id      UUID        NOT NULL,
            entity_id      UUID        NOT NULL,
            entity_type    VARCHAR(50) NOT NULL,
            last_ledger_id BIGINT      NOT NULL
                           REFERENCES public.address_audit_ledger (id),
            last_hmac      CHAR(64)    NOT NULL,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, entity_id, entity_type)
        ) WITH (fillfactor = 80)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.audit_chain_heads CASCADE")
    op.execute("DROP TABLE IF EXISTS public.address_audit_ledger CASCADE")
    op.execute("DROP TABLE IF EXISTS public.organization_address_payloads_secure CASCADE")
    op.execute("DROP TABLE IF EXISTS public.encryption_key_registry CASCADE")
