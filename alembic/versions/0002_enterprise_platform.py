"""
Alembic migration: enterprise platform tables
=============================================
Creates:
  • public.active_idempotency_keys   — non-partitioned anchor for uniqueness enforcement
  • public.idempotency_store         — RANGE-partitioned payload store (weekly)
  • public.key_rotation_progress     — resumable DEK rotation watermark cursors
  • public.tenant_resource_quotas    — per-tenant resource limits
  • public.event_outbox              — transactional outbox (weekly RANGE partitions)
  • public.event_outbox_delivery_state — delivery tracking (HASH partitioned)

Revision: 0002_enterprise_platform
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP

revision = "0002_enterprise_platform"
down_revision = "371b1a44a334"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── active_idempotency_keys (uniqueness anchor) ──────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.active_idempotency_keys (
            tenant_id         UUID         NOT NULL,
            idempotency_key   VARCHAR(255) NOT NULL,
            status            VARCHAR(20)  NOT NULL DEFAULT 'IN_PROGRESS',
            heartbeat_at      TIMESTAMPTZ  NULL,
            owner_worker_id   UUID         NULL,
            partition_name    TEXT         NOT NULL DEFAULT '',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, idempotency_key)
        ) WITH (fillfactor = 80)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_idempotency_zombie
            ON public.active_idempotency_keys (status, heartbeat_at)
            WHERE status = 'IN_PROGRESS'
    """)

    # ── idempotency_store (RANGE partitioned by created_at) ──────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.idempotency_store (
            tenant_id               UUID         NOT NULL,
            idempotency_key         VARCHAR(255) NOT NULL,
            request_hash            CHAR(64)     NOT NULL,
            status                  VARCHAR(20)  NOT NULL DEFAULT 'IN_PROGRESS',
            response_code           SMALLINT     NULL,
            response_payload        BYTEA        NULL,
            response_payload_ref    TEXT         NULL,
            response_payload_sha256 BYTEA        NULL,
            response_hmac           CHAR(64)     NULL,
            created_at              TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, idempotency_key, created_at)
        ) PARTITION BY RANGE (created_at)
    """)

    # Create initial partition covering the current week
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.idempotency_store_y2026_m05_d18
        PARTITION OF public.idempotency_store
        FOR VALUES FROM ('2026-05-18 00:00:00+00') TO ('2026-05-25 00:00:00+00')
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_idempotency_store_created_brin
            ON public.idempotency_store USING BRIN (created_at)
    """)

    # ── key_rotation_progress ────────────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.key_rotation_progress (
            tenant_id        UUID         NOT NULL,
            table_name       VARCHAR(100) NOT NULL,
            last_processed_pk UUID        NOT NULL,
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, table_name)
        )
    """)

    # ── tenant_resource_quotas ───────────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.tenant_resource_quotas (
            tenant_id                   UUID    PRIMARY KEY,
            max_writes_per_minute       INTEGER NOT NULL DEFAULT 600,
            max_outbox_events_per_hour  INTEGER NOT NULL DEFAULT 50000,
            max_storage_bytes           BIGINT  NOT NULL DEFAULT 10737418240,
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
        )
    """)

    # ── event_outbox ─────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.event_outbox (
            event_id       UUID        NOT NULL DEFAULT pg_catalog.gen_random_uuid(),
            event_type     VARCHAR(100) NOT NULL,
            payload        JSONB       NOT NULL,
            event_version  SMALLINT    NOT NULL DEFAULT 1,
            tenant_id      UUID        NOT NULL,
            lineage_id     UUID        NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (created_at, tenant_id, event_id)
        ) PARTITION BY RANGE (created_at)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.event_outbox_y2026_m05_d18
        PARTITION OF public.event_outbox
        FOR VALUES FROM ('2026-05-18 00:00:00+00') TO ('2026-05-25 00:00:00+00')
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_event_outbox_consumer
            ON public.event_outbox (tenant_id, created_at)
    """)

    # ── event_outbox_delivery_state ──────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.event_outbox_delivery_state (
            event_id       UUID        NOT NULL,
            tenant_id      UUID        NOT NULL,
            status         VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            locked_at      TIMESTAMPTZ NULL,
            locked_by      UUID        NULL,
            next_retry_at  TIMESTAMPTZ NULL,
            retry_count    INT         NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMPTZ NULL,
            failure_reason TEXT        NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, event_id)
        ) PARTITION BY HASH (tenant_id)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.event_outbox_delivery_state_p0
        PARTITION OF public.event_outbox_delivery_state
        FOR VALUES WITH (MODULUS 8, REMAINDER 0)
    """)
    for i in range(1, 8):
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS public.event_outbox_delivery_state_p{i}
            PARTITION OF public.event_outbox_delivery_state
            FOR VALUES WITH (MODULUS 8, REMAINDER {i})
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.event_outbox_delivery_state CASCADE")
    op.execute("DROP TABLE IF EXISTS public.event_outbox CASCADE")
    op.execute("DROP TABLE IF EXISTS public.tenant_resource_quotas CASCADE")
    op.execute("DROP TABLE IF EXISTS public.key_rotation_progress CASCADE")
    op.execute("DROP TABLE IF EXISTS public.idempotency_store CASCADE")
    op.execute("DROP TABLE IF EXISTS public.active_idempotency_keys CASCADE")
