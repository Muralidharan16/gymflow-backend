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
depends_on = None


_TARGET_RELATIONS = (
    "active_idempotency_keys",
    "idx_idempotency_zombie",
    "idempotency_store",
    "idempotency_store_y2026_m05_d18",
    "idx_idempotency_store_created_brin",
    "key_rotation_progress",
    "tenant_resource_quotas",
    "event_outbox",
    "event_outbox_y2026_m05_d18",
    "idx_event_outbox_consumer",
    "event_outbox_delivery_state",
    "event_outbox_delivery_state_p0",
    "event_outbox_delivery_state_p1",
    "event_outbox_delivery_state_p2",
    "event_outbox_delivery_state_p3",
    "event_outbox_delivery_state_p4",
    "event_outbox_delivery_state_p5",
    "event_outbox_delivery_state_p6",
    "event_outbox_delivery_state_p7",
)

_DATA_RELATIONS = (
    "active_idempotency_keys",
    "idempotency_store",
    "key_rotation_progress",
    "tenant_resource_quotas",
    "event_outbox",
    "event_outbox_delivery_state",
)


def _preflight_upgrade() -> None:
    """Refuse to adopt colliding objects not owned by this revision."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'active_idempotency_keys',
                'idx_idempotency_zombie',
                'idempotency_store',
                'idempotency_store_y2026_m05_d18',
                'idx_idempotency_store_created_brin',
                'key_rotation_progress',
                'tenant_resource_quotas',
                'event_outbox',
                'event_outbox_y2026_m05_d18',
                'idx_event_outbox_consumer',
                'event_outbox_delivery_state',
                'event_outbox_delivery_state_p0',
                'event_outbox_delivery_state_p1',
                'event_outbox_delivery_state_p2',
                'event_outbox_delivery_state_p3',
                'event_outbox_delivery_state_p4',
                'event_outbox_delivery_state_p5',
                'event_outbox_delivery_state_p6',
                'event_outbox_delivery_state_p7'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NOT NULL THEN
                    RAISE EXCEPTION
                        '0002 target relation public.% already exists; refusing adoption',
                        relation_name;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def _preflight_downgrade() -> None:
    """Fail closed if predecessor 371b1a44a334 cannot represent 0002 data."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
            has_rows boolean;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'active_idempotency_keys',
                'idempotency_store',
                'key_rotation_progress',
                'tenant_resource_quotas',
                'event_outbox',
                'event_outbox_delivery_state'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NULL THEN
                    RAISE EXCEPTION
                        '0002 downgrade predecessor drift: required relation public.% is missing',
                        relation_name;
                END IF;

                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)',
                    relation_name
                ) INTO has_rows;

                IF has_rows THEN
                    RAISE EXCEPTION
                        '0002 downgrade would discard populated revision-owned relation public.%',
                        relation_name;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight_upgrade()

    # ── active_idempotency_keys (uniqueness anchor) ──────────────────────

    op.execute("""
        CREATE TABLE public.active_idempotency_keys (
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
        CREATE INDEX idx_idempotency_zombie
            ON public.active_idempotency_keys (status, heartbeat_at)
            WHERE status = 'IN_PROGRESS'
    """)

    # ── idempotency_store (RANGE partitioned by created_at) ──────────────

    op.execute("""
        CREATE TABLE public.idempotency_store (
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
        CREATE TABLE public.idempotency_store_y2026_m05_d18
        PARTITION OF public.idempotency_store
        FOR VALUES FROM ('2026-05-18 00:00:00+00') TO ('2026-05-25 00:00:00+00')
    """)

    op.execute("""
        CREATE INDEX idx_idempotency_store_created_brin
            ON public.idempotency_store USING BRIN (created_at)
    """)

    # ── key_rotation_progress ────────────────────────────────────────────

    op.execute("""
        CREATE TABLE public.key_rotation_progress (
            tenant_id        UUID         NOT NULL,
            table_name       VARCHAR(100) NOT NULL,
            last_processed_pk UUID        NOT NULL,
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (tenant_id, table_name)
        )
    """)

    # ── tenant_resource_quotas ───────────────────────────────────────────

    op.execute("""
        CREATE TABLE public.tenant_resource_quotas (
            tenant_id                   UUID    PRIMARY KEY,
            max_writes_per_minute       INTEGER NOT NULL DEFAULT 600,
            max_outbox_events_per_hour  INTEGER NOT NULL DEFAULT 50000,
            max_storage_bytes           BIGINT  NOT NULL DEFAULT 10737418240,
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
        )
    """)

    # ── event_outbox ─────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE public.event_outbox (
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
        CREATE TABLE public.event_outbox_y2026_m05_d18
        PARTITION OF public.event_outbox
        FOR VALUES FROM ('2026-05-18 00:00:00+00') TO ('2026-05-25 00:00:00+00')
    """)

    op.execute("""
        CREATE INDEX idx_event_outbox_consumer
            ON public.event_outbox (tenant_id, created_at)
    """)

    # ── event_outbox_delivery_state ──────────────────────────────────────

    op.execute("""
        CREATE TABLE public.event_outbox_delivery_state (
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
        CREATE TABLE public.event_outbox_delivery_state_p0
        PARTITION OF public.event_outbox_delivery_state
        FOR VALUES WITH (MODULUS 8, REMAINDER 0)
    """)
    for i in range(1, 8):
        op.execute(f"""
            CREATE TABLE public.event_outbox_delivery_state_p{i}
            PARTITION OF public.event_outbox_delivery_state
            FOR VALUES WITH (MODULUS 8, REMAINDER {i})
        """)


def downgrade() -> None:
    _preflight_downgrade()

    # RESTRICT is intentional: if an unexpected later/manual dependency still
    # exists, rollback must fail visibly instead of CASCADE-dropping it.
    op.execute("DROP TABLE public.event_outbox_delivery_state RESTRICT")
    op.execute("DROP TABLE public.event_outbox RESTRICT")
    op.execute("DROP TABLE public.tenant_resource_quotas RESTRICT")
    op.execute("DROP TABLE public.key_rotation_progress RESTRICT")
    op.execute("DROP TABLE public.idempotency_store RESTRICT")
    op.execute("DROP TABLE public.active_idempotency_keys RESTRICT")
