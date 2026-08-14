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

revision = "0003_security_schemas"
down_revision = "0002_enterprise_platform"
branch_labels = None
depends_on = None


def _preflight_upgrade() -> None:
    """Refuse to adopt colliding security/audit objects."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'encryption_key_registry',
                'encryption_key_registry_key_version_seq',
                'uq_key_registry_active',
                'idx_key_registry_tenant_ver',
                'organization_address_payloads_secure',
                'idx_addr_payloads_tenant_org',
                'idx_addr_payloads_key_version',
                'idx_addr_payloads_created_brin',
                'address_audit_ledger',
                'address_audit_ledger_id_seq',
                'idx_audit_ledger_entity',
                'idx_audit_ledger_changed_brin',
                'audit_chain_heads'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NOT NULL THEN
                    RAISE EXCEPTION
                        '0003 target relation public.% already exists; refusing adoption',
                        relation_name;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def _preflight_downgrade() -> None:
    """Refuse rollback when 0003 security/audit data cannot be represented."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
            has_rows boolean;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'encryption_key_registry',
                'organization_address_payloads_secure',
                'address_audit_ledger',
                'audit_chain_heads'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NULL THEN
                    RAISE EXCEPTION
                        '0003 downgrade predecessor drift: required relation public.% is missing',
                        relation_name;
                END IF;

                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)',
                    relation_name
                ) INTO has_rows;

                IF has_rows THEN
                    RAISE EXCEPTION
                        '0003 downgrade would discard populated security/audit relation public.%',
                        relation_name;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight_upgrade()

    # ── encryption_key_registry ──────────────────────────────────────────

    op.execute("""
        CREATE TABLE public.encryption_key_registry (
            key_version     SERIAL       PRIMARY KEY,
            tenant_id       UUID         NOT NULL,
            table_name      VARCHAR(100) NOT NULL,
            encrypted_dek   BYTEA        NOT NULL,
            key_status      VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE'
                            CHECK (key_status IN ('ACTIVE','DEPRECATED','RETIRED')),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            deprecated_at   TIMESTAMPTZ  NULL,
            retired_at      TIMESTAMPTZ  NULL
        ) WITH (fillfactor = 90)
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_key_registry_active
            ON public.encryption_key_registry (tenant_id, table_name)
            WHERE key_status = 'ACTIVE'
    """)

    op.execute("""
        CREATE INDEX idx_key_registry_tenant_ver
            ON public.encryption_key_registry (tenant_id, key_version)
    """)

    # ── organization_address_payloads_secure ─────────────────────────────

    op.execute("""
        CREATE TABLE public.organization_address_payloads_secure (
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
        CREATE INDEX idx_addr_payloads_tenant_org
            ON public.organization_address_payloads_secure (tenant_id, org_id)
    """)

    op.execute("""
        CREATE INDEX idx_addr_payloads_key_version
            ON public.organization_address_payloads_secure (key_version)
            WHERE key_version > 0
    """)

    op.execute("""
        CREATE INDEX idx_addr_payloads_created_brin
            ON public.organization_address_payloads_secure USING BRIN (created_at)
    """)

    # ── address_audit_ledger (append-only, HMAC-chained) ─────────────────

    op.execute("""
        CREATE TABLE public.address_audit_ledger (
            id              BIGSERIAL    PRIMARY KEY,
            tenant_id       UUID         NOT NULL,
            entity_id       UUID         NOT NULL,
            entity_type     VARCHAR(50)  NOT NULL,
            event_type      VARCHAR(50)  NOT NULL,
            event_version   SMALLINT     NOT NULL DEFAULT 1,
            changed_by      UUID         NOT NULL,
            changed_at      TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            payload_hash    CHAR(64)     NOT NULL,
            chain_hmac      CHAR(64)     NOT NULL,
            prev_chain_hmac CHAR(64)     NULL,
            ip_address      VARCHAR(45)  NULL,
            trace_id        VARCHAR(128) NULL,
            metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb
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
        CREATE INDEX idx_audit_ledger_entity
            ON public.address_audit_ledger (tenant_id, entity_id, changed_at)
    """)

    op.execute("""
        CREATE INDEX idx_audit_ledger_changed_brin
            ON public.address_audit_ledger USING BRIN (changed_at)
    """)

    # ── audit_chain_heads ────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE public.audit_chain_heads (
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
    _preflight_downgrade()

    # Drop children before parents and use RESTRICT so unexpected external
    # dependencies fail visibly instead of being recursively destroyed.
    op.execute("DROP TABLE public.audit_chain_heads RESTRICT")
    op.execute("DROP TABLE public.address_audit_ledger RESTRICT")
    op.execute("DROP TABLE public.organization_address_payloads_secure RESTRICT")
    op.execute("DROP TABLE public.encryption_key_registry RESTRICT")
