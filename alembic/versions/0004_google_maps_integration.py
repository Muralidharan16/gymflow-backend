"""
Add Google Maps integration to organization_addresses

Revision: 0004_google_maps_integration
Down revision: 0003_security_schemas
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import TIMESTAMP, DOUBLE_PRECISION

revision = "0004_google_maps_integration"
down_revision = "0003_security_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 11 columns on organization_addresses ────────────────────────────

    op.add_column("organization_addresses",
        sa.Column("google_place_id", sa.String(300), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("latitude", DOUBLE_PRECISION(), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("longitude", DOUBLE_PRECISION(), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_embed_allowed", sa.Boolean(),
                  nullable=False, server_default="true"))
    op.add_column("organization_addresses",
        sa.Column("maps_verification_status", sa.String(30),
                  nullable=False, server_default="pending"))
    op.add_column("organization_addresses",
        sa.Column("maps_last_verified_at",
                  TIMESTAMP(timezone=True), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_verification_error", sa.Text(), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_verification_source", sa.String(50), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_updated_at",
                  TIMESTAMP(timezone=True), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_next_retry_at",
                  TIMESTAMP(timezone=True), nullable=True))
    op.add_column("organization_addresses",
        sa.Column("maps_retry_count", sa.Integer(),
                  nullable=False, server_default="0"))

    # ── 7 CHECK constraints (NOT VALID + VALIDATE) ──────────────────────

    constraints = [
        ("chk_latitude",
         "latitude BETWEEN -90 AND 90"),

        ("chk_longitude",
         "longitude BETWEEN -180 AND 180"),

        ("chk_maps_verification_status",
         "maps_verification_status IN "
         "('pending','verified','failed','stale','disabled')"),

        ("chk_verified_maps_have_coordinates",
         "maps_verification_status != 'verified' "
         "OR (latitude IS NOT NULL AND longitude IS NOT NULL)"),

        ("chk_maps_retry_count",
         "maps_retry_count >= 0 AND maps_retry_count <= 100"),

        ("chk_maps_verification_error",
         "maps_verification_error IS NULL OR "
         "maps_verification_error IN ("
         "'GOOGLE_TIMEOUT','GOOGLE_QUOTA_EXCEEDED','PLACE_NOT_FOUND',"
         "'INVALID_PLACE_ID','NETWORK_ERROR','API_DISABLED',"
         "'BILLING_ERROR','PLACE_REMOVED')"),

        ("chk_maps_verification_source",
         "maps_verification_source IS NULL OR "
         "maps_verification_source IN ("
         "'google_places_api','geocoding_api','manual_override',"
         "'legacy_import','cache_rehydration')"),
    ]

    for name, expr in constraints:
        op.execute(f"""
            ALTER TABLE organization_addresses
            ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID
        """)
        op.execute(f"""
            ALTER TABLE organization_addresses
            VALIDATE CONSTRAINT {name}
        """)

    # ── 4 indexes (CONCURRENTLY) ────────────────────────────────────────

    with op.get_context().autocommit_block():
        op.execute("""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                uq_org_addresses_place_id
                ON organization_addresses (org_id, google_place_id)
                WHERE google_place_id IS NOT NULL
                  AND deleted_at IS NULL
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_org_addresses_lat_lng
                ON organization_addresses (latitude, longitude)
                WHERE latitude IS NOT NULL
                  AND longitude IS NOT NULL
                  AND deleted_at IS NULL
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_org_addresses_verification_status
                ON organization_addresses (maps_verification_status)
                WHERE maps_verification_status IN ('pending', 'stale')
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_org_addresses_next_retry
                ON organization_addresses (maps_next_retry_at)
                WHERE maps_next_retry_at IS NOT NULL
                  AND deleted_at IS NULL
        """)

    # ── google_places_cache table ───────────────────────────────────────

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.google_places_cache (
            place_id          VARCHAR(300)     PRIMARY KEY,
            latitude          DOUBLE PRECISION NOT NULL,
            longitude         DOUBLE PRECISION NOT NULL,
            formatted_address TEXT             NULL,
            place_name        VARCHAR(255)     NULL,
            place_types       JSONB            NULL,
            verified_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
            expires_at        TIMESTAMPTZ      NOT NULL
                              DEFAULT (now() + interval '30 days'),
            created_at        TIMESTAMPTZ      NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ      NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_places_cache_expires
            ON google_places_cache (expires_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS google_places_cache CASCADE")

    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_org_addresses_next_retry")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_org_addresses_verification_status")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_org_addresses_lat_lng")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_org_addresses_place_id")

    for name in [
        "chk_maps_verification_source",
        "chk_maps_verification_error",
        "chk_maps_retry_count",
        "chk_verified_maps_have_coordinates",
        "chk_maps_verification_status",
        "chk_longitude",
        "chk_latitude",
    ]:
        op.execute(f"ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS {name}")

    for col in [
        "maps_retry_count", "maps_next_retry_at", "maps_updated_at",
        "maps_verification_source", "maps_verification_error",
        "maps_last_verified_at", "maps_verification_status",
        "maps_embed_allowed", "longitude", "latitude", "google_place_id",
    ]:
        op.drop_column("organization_addresses", col)
