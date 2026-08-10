"""add_hyperscale_branch_name_and_address_architecture

Revision ID: 00f277c748ea
Revises: 0009_view_security_invoker
Create Date: 2026-05-22 18:53:22.258447

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '00f277c748ea'
down_revision: Union[str, Sequence[str], None] = '0009_view_security_invoker'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TARGET_RELATIONS = (
    'address_change_outbox',
    'branch_address_audit_log',
    'branch_name_translations',
    'branch_address_history',
    'branch_geocode_attempts',
    'branch_geolocation_state',
)


def _preflight_upgrade() -> None:
    """Fail before mutation when the predecessor cannot be migrated safely."""
    op.execute(r"""
        DO $$
        DECLARE
            collision text;
            expected_extension text;
        BEGIN
            IF current_user <> 'migration_owner' THEN
                RAISE EXCEPTION
                    '00f migration must execute as migration_owner, got %', current_user;
            END IF;

            SELECT relation_data.relname
            INTO collision
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname IN (
                  'address_change_outbox',
                  'branch_address_audit_log',
                  'branch_name_translations',
                  'branch_address_history',
                  'branch_geocode_attempts',
                  'branch_geolocation_state'
              )
            ORDER BY relation_data.relname
            LIMIT 1;

            IF collision IS NOT NULL THEN
                RAISE EXCEPTION
                    '00f target relation public.% already exists; refusing destructive adoption',
                    collision;
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.organization_addresses
                WHERE address_type NOT IN ('registered', 'operational', 'billing')
            ) THEN
                RAISE EXCEPTION
                    '00f predecessor contains unsupported organization address_type values';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.organization_addresses
                WHERE length(
                    COALESCE(
                        NULLIF(maps_verification_source, ''),
                        NULLIF(verification_source, ''),
                        NULLIF(coordinates_source, ''),
                        ''
                    )
                ) > 20
            ) THEN
                RAISE EXCEPTION
                    '00f cannot preserve a geocode source longer than 20 characters; reconcile source values before migration';
            END IF;

            FOREACH expected_extension IN ARRAY ARRAY[
                'btree_gist', 'citext', 'pg_trgm', 'postgis'
            ] LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_extension AS extension_data
                    JOIN pg_catalog.pg_roles AS owner_role
                      ON owner_role.oid = extension_data.extowner
                    WHERE extension_data.extname = expected_extension
                      AND owner_role.rolname = 'postgres'
                ) THEN
                    RAISE EXCEPTION
                        '00f requires infrastructure-owned extension %, provision it before Alembic',
                        expected_extension;
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM (VALUES
                    ('branch_admin'),
                    ('branch_viewer'),
                    ('ops_support')
                ) AS required(role_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles AS role_data
                    WHERE role_data.rolname = required.role_name
                )
            ) THEN
                RAISE EXCEPTION
                    'Required managed cluster roles are missing; security/cluster_role_bootstrap contract must be applied before Alembic migrations.';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS role_data
                WHERE role_data.rolname IN ('branch_admin', 'branch_viewer', 'ops_support')
                  AND (
                        role_data.rolsuper
                     OR role_data.rolbypassrls
                     OR role_data.rolcanlogin
                     OR role_data.rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'Managed cluster role attributes violate the approved security/cluster_role_bootstrap contract.';
            END IF;
        END
        $$;
    """)


def _backfill_legacy_addresses() -> None:
    """Map predecessor org-level addresses into the branch architecture.

    FORCE RLS is already active on the branch tables at revision 0009.  The
    migration therefore works one organization at a time and sets only the
    tenant GUC required by the predecessor policies.  It never disables RLS or
    uses BYPASSRLS.
    """
    op.execute(r"""
        DO $$
        DECLARE
            org_row record;
            address_row record;
            branch_count integer;
            primary_branch_count integer;
            unmatched_physical_count integer;
            active_physical_count integer;
            fallback_branch_id uuid;
            canonical_address_id uuid;
            new_branch_id uuid;
            ordinal integer;
        BEGIN
            FOR org_row IN
                SELECT organization_data.id,
                       organization_data.name,
                       organization_data.max_branches
                FROM public.organizations AS organization_data
                WHERE EXISTS (
                    SELECT 1
                    FROM public.organization_addresses AS address_data
                    WHERE address_data.org_id = organization_data.id
                )
                ORDER BY organization_data.id
            LOOP
                PERFORM pg_catalog.set_config(
                    'app.current_org_id', org_row.id::text, true
                );

                SELECT address_data.id
                INTO canonical_address_id
                FROM public.organization_addresses AS address_data
                WHERE address_data.org_id = org_row.id
                ORDER BY
                    (address_data.deleted_at IS NULL) DESC,
                    address_data.is_primary DESC,
                    (address_data.address_type = 'physical') DESC,
                    address_data.created_at,
                    address_data.id
                LIMIT 1;

                SELECT count(*)
                INTO branch_count
                FROM public.org_branches AS branch_data
                WHERE branch_data.org_id = org_row.id;

                IF branch_count = 0 THEN
                    SELECT count(*)
                    INTO active_physical_count
                    FROM public.organization_addresses AS address_data
                    WHERE address_data.org_id = org_row.id
                      AND address_data.address_type = 'physical'
                      AND address_data.deleted_at IS NULL;

                    IF GREATEST(active_physical_count, 1) > org_row.max_branches THEN
                        RAISE EXCEPTION
                            '00f requires % migrated branches for organization %, exceeding max_branches=%',
                            GREATEST(active_physical_count, 1),
                            org_row.id,
                            org_row.max_branches;
                    END IF;

                    ordinal := 0;
                    fallback_branch_id := NULL;

                    FOR address_row IN
                        SELECT address_data.id,
                               address_data.label,
                               address_data.country_code,
                               address_data.is_primary,
                               address_data.created_at
                        FROM public.organization_addresses AS address_data
                        WHERE address_data.org_id = org_row.id
                          AND address_data.address_type = 'physical'
                          AND address_data.deleted_at IS NULL
                        ORDER BY address_data.is_primary DESC,
                                 address_data.created_at,
                                 address_data.id
                    LOOP
                        ordinal := ordinal + 1;
                        new_branch_id := (
                            md5(
                                org_row.id::text || ':' ||
                                address_row.id::text || '-00f-legacy-branch'
                            )
                        )::uuid;

                        IF EXISTS (
                            SELECT 1
                            FROM public.org_branches AS existing_branch
                            WHERE existing_branch.id = new_branch_id
                        ) THEN
                            RAISE EXCEPTION
                                '00f deterministic branch id collision for organization %, address %',
                                org_row.id,
                                address_row.id;
                        END IF;

                        INSERT INTO public.org_branches (
                            id,
                            org_id,
                            branch_name,
                            branch_code,
                            internal_slug,
                            country_code,
                            address_id,
                            branch_metadata
                        ) VALUES (
                            new_branch_id,
                            org_row.id,
                            CASE
                                WHEN ordinal = 1 THEN
                                    left(
                                        COALESCE(
                                            NULLIF(address_row.label, ''),
                                            NULLIF(org_row.name, ''),
                                            'Main Branch'
                                        ),
                                        120
                                    )
                                ELSE
                                    'Legacy Location ' ||
                                    substr(replace(address_row.id::text, '-', ''), 1, 8)
                            END,
                            CASE
                                WHEN ordinal = 1 THEN 'MAIN'
                                ELSE 'LEG-' || upper(
                                    substr(replace(address_row.id::text, '-', ''), 1, 12)
                                )
                            END,
                            CASE
                                WHEN ordinal = 1 THEN 'main'
                                ELSE 'legacy-' || lower(
                                    substr(replace(address_row.id::text, '-', ''), 1, 12)
                                )
                            END,
                            address_row.country_code,
                            address_row.id,
                            jsonb_build_object(
                                'migration_00f_legacy_backfill', true,
                                'legacy_address_id', address_row.id
                            )
                        );

                        INSERT INTO public.org_branch_state (
                            branch_id,
                            org_id,
                            branch_status,
                            is_primary,
                            is_active,
                            is_public,
                            search_epoch_ulid
                        ) VALUES (
                            new_branch_id,
                            org_row.id,
                            'active',
                            ordinal = 1,
                            true,
                            true,
                            '00000000000000000000000000'
                        );

                        UPDATE public.organization_addresses
                        SET branch_id = new_branch_id
                        WHERE id = address_row.id
                          AND org_id = org_row.id;

                        IF ordinal = 1 THEN
                            fallback_branch_id := new_branch_id;
                        END IF;
                    END LOOP;

                    IF active_physical_count = 0 THEN
                        new_branch_id := (
                            md5(
                                org_row.id::text || ':' ||
                                canonical_address_id::text || '-00f-legacy-main'
                            )
                        )::uuid;

                        INSERT INTO public.org_branches (
                            id,
                            org_id,
                            branch_name,
                            branch_code,
                            internal_slug,
                            country_code,
                            address_id,
                            branch_metadata
                        )
                        SELECT
                            new_branch_id,
                            org_row.id,
                            left(
                                COALESCE(
                                    NULLIF(address_data.label, ''),
                                    NULLIF(org_row.name, ''),
                                    'Main Branch'
                                ),
                                120
                            ),
                            'MAIN',
                            'main',
                            address_data.country_code,
                            address_data.id,
                            jsonb_build_object(
                                'migration_00f_legacy_backfill', true,
                                'legacy_address_id', address_data.id
                            )
                        FROM public.organization_addresses AS address_data
                        WHERE address_data.id = canonical_address_id
                          AND address_data.org_id = org_row.id;

                        INSERT INTO public.org_branch_state (
                            branch_id,
                            org_id,
                            branch_status,
                            is_primary,
                            is_active,
                            is_public,
                            search_epoch_ulid
                        ) VALUES (
                            new_branch_id,
                            org_row.id,
                            CASE
                                WHEN EXISTS (
                                    SELECT 1
                                    FROM public.organization_addresses AS address_data
                                    WHERE address_data.id = canonical_address_id
                                      AND address_data.deleted_at IS NULL
                                ) THEN 'active'
                                ELSE 'inactive'
                            END,
                            true,
                            EXISTS (
                                SELECT 1
                                FROM public.organization_addresses AS address_data
                                WHERE address_data.id = canonical_address_id
                                  AND address_data.deleted_at IS NULL
                            ),
                            true,
                            '00000000000000000000000000'
                        );

                        fallback_branch_id := new_branch_id;
                    END IF;

                    UPDATE public.organization_addresses AS address_data
                    SET branch_id = fallback_branch_id
                    WHERE address_data.org_id = org_row.id
                      AND address_data.branch_id IS NULL;
                ELSE
                    IF EXISTS (
                        SELECT branch_data.address_id
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.org_id = org_row.id
                          AND branch_data.address_id IS NOT NULL
                        GROUP BY branch_data.address_id
                        HAVING count(*) > 1
                    ) THEN
                        RAISE EXCEPTION
                            '00f found multiple branches referencing the same legacy address in organization %',
                            org_row.id;
                    END IF;

                    UPDATE public.organization_addresses AS address_data
                    SET branch_id = branch_data.id
                    FROM public.org_branches AS branch_data
                    WHERE branch_data.org_id = org_row.id
                      AND branch_data.address_id = address_data.id
                      AND address_data.org_id = org_row.id;

                    SELECT count(*)
                    INTO primary_branch_count
                    FROM public.org_branch_state AS state_data
                    WHERE state_data.org_id = org_row.id
                      AND state_data.is_primary
                      AND state_data.deleted_at IS NULL;

                    fallback_branch_id := NULL;

                    IF primary_branch_count = 1 THEN
                        SELECT state_data.branch_id
                        INTO fallback_branch_id
                        FROM public.org_branch_state AS state_data
                        WHERE state_data.org_id = org_row.id
                          AND state_data.is_primary
                          AND state_data.deleted_at IS NULL;
                    ELSIF branch_count = 1 THEN
                        SELECT branch_data.id
                        INTO fallback_branch_id
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.org_id = org_row.id;
                    ELSIF canonical_address_id IS NOT NULL THEN
                        SELECT branch_data.id
                        INTO fallback_branch_id
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.org_id = org_row.id
                          AND branch_data.address_id = canonical_address_id;
                    END IF;

                    SELECT count(*)
                    INTO unmatched_physical_count
                    FROM public.organization_addresses AS address_data
                    WHERE address_data.org_id = org_row.id
                      AND address_data.branch_id IS NULL
                      AND address_data.address_type = 'physical'
                      AND address_data.deleted_at IS NULL;

                    IF unmatched_physical_count > 1 THEN
                        RAISE EXCEPTION
                            '00f cannot safely map % unmatched active operational addresses across existing branches for organization %',
                            unmatched_physical_count,
                            org_row.id;
                    END IF;

                    IF EXISTS (
                        SELECT 1
                        FROM public.organization_addresses AS address_data
                        WHERE address_data.org_id = org_row.id
                          AND address_data.branch_id IS NULL
                    ) AND fallback_branch_id IS NULL THEN
                        RAISE EXCEPTION
                            '00f cannot determine an unambiguous target branch for legacy addresses in organization %',
                            org_row.id;
                    END IF;

                    UPDATE public.organization_addresses AS address_data
                    SET branch_id = fallback_branch_id
                    WHERE address_data.org_id = org_row.id
                      AND address_data.branch_id IS NULL;
                END IF;
            END LOOP;

            -- org_branches is already FORCE RLS at revision 0009.  Validate
            -- mapping integrity one tenant at a time while that tenant's GUC is
            -- active; a global join after clearing the GUC would falsely hide
            -- every protected branch row.
            FOR org_row IN
                SELECT DISTINCT address_data.org_id AS id
                FROM public.organization_addresses AS address_data
                ORDER BY address_data.org_id
            LOOP
                PERFORM pg_catalog.set_config(
                    'app.current_org_id', org_row.id::text, true
                );

                IF EXISTS (
                    SELECT 1
                    FROM public.organization_addresses AS address_data
                    LEFT JOIN public.org_branches AS branch_data
                      ON branch_data.id = address_data.branch_id
                    WHERE address_data.org_id = org_row.id
                      AND (
                           address_data.branch_id IS NULL
                        OR branch_data.id IS NULL
                        OR branch_data.org_id <> address_data.org_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        '00f branch backfill left a null, missing, or cross-tenant address mapping for organization %',
                        org_row.id;
                END IF;
            END LOOP;

            PERFORM pg_catalog.set_config(
                'app.current_org_id',
                '00000000-0000-0000-0000-000000000000',
                true
            );

            -- organization_addresses is not FORCE-RLS until later in this
            -- revision, so this final check safely proves there are no null
            -- branch assignments without attempting an RLS-protected join.
            IF EXISTS (
                SELECT 1
                FROM public.organization_addresses AS address_data
                WHERE address_data.branch_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '00f branch backfill left a null branch assignment';
            END IF;
        END
        $$;
    """)

    op.execute(r"""
        INSERT INTO public.branch_geolocation_state (
            address_id,
            org_id,
            coordinates,
            last_known_good_coordinates,
            validation_status,
            geocode_attempts,
            last_geocode_attempt_at,
            next_retry_at,
            geocoded_at,
            geocode_provider
        )
        SELECT
            source_data.id,
            source_data.org_id,
            source_data.coordinate_text,
            CASE
                WHEN source_data.coordinate_text IS NOT NULL
                 AND (
                     source_data.is_verified
                     OR source_data.maps_verification_status = 'verified'
                 )
                THEN source_data.coordinate_text
                ELSE NULL
            END,
            CASE
                WHEN source_data.coordinate_text IS NOT NULL
                 AND (
                     source_data.is_verified
                     OR source_data.maps_verification_status = 'verified'
                 ) THEN 'success'
                WHEN source_data.geocoding_failed
                  OR source_data.maps_verification_status = 'failed' THEN 'failed'
                WHEN source_data.maps_verification_status = 'disabled' THEN 'skipped'
                ELSE 'pending'
            END,
            source_data.maps_retry_count,
            source_data.maps_updated_at,
            source_data.maps_next_retry_at,
            CASE
                WHEN source_data.coordinate_text IS NOT NULL
                 AND (
                     source_data.is_verified
                     OR source_data.maps_verification_status = 'verified'
                 )
                THEN COALESCE(
                    source_data.maps_last_verified_at,
                    source_data.verified_at
                )
                ELSE NULL
            END,
            NULLIF(
                COALESCE(
                    NULLIF(source_data.maps_verification_source, ''),
                    NULLIF(source_data.verification_source, ''),
                    NULLIF(source_data.coordinates_source, '')
                ),
                ''
            )
        FROM (
            SELECT
                address_data.*,
                CASE
                    WHEN address_data.coordinates IS NOT NULL THEN
                        ST_AsText(address_data.coordinates::geometry)
                    WHEN address_data.latitude IS NOT NULL
                     AND address_data.longitude IS NOT NULL THEN
                        format(
                            'POINT(%s %s)',
                            address_data.longitude,
                            address_data.latitude
                        )
                    ELSE NULL
                END AS coordinate_text
            FROM public.organization_addresses AS address_data
        ) AS source_data;

        DO $$
        BEGIN
            IF (
                SELECT count(*)
                FROM public.branch_geolocation_state
            ) <> (
                SELECT count(*)
                FROM public.organization_addresses
            ) THEN
                RAISE EXCEPTION
                    '00f geolocation backfill did not create exactly one state row per organization address';
            END IF;
        END
        $$;
    """)


def upgrade() -> None:
    """Upgrade schema without discarding populated predecessor data."""
    _preflight_upgrade()

    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS ck_org_address_type;")
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS chk_address_type;")
    op.execute("UPDATE organization_addresses SET address_type = 'physical' WHERE address_type = 'operational';")
    op.execute("ALTER TABLE organization_addresses ADD CONSTRAINT chk_address_type CHECK (address_type IN ('physical', 'mailing', 'billing', 'registered'));")

    # Drop view temporarily to allow altering column types.
    op.execute("DROP VIEW IF EXISTS v_active_org_branches;")

    op.create_table('address_change_outbox',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('address_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('branch_address_audit_log',
    sa.Column('event_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('address_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('dek_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('old_address', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('new_address', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('changed_by', sa.UUID(), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('request_id', sa.UUID(), nullable=True),
    sa.Column('changed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_table('branch_name_translations',
    sa.Column('branch_id', sa.UUID(), nullable=False),
    sa.Column('locale', sa.String(length=10), nullable=False),
    sa.Column('branch_name', sa.String(length=120), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', branch_name)", persisted=True), nullable=True),
    sa.ForeignKeyConstraint(['branch_id'], ['org_branches.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('branch_id', 'locale')
    )
    op.create_index('ix_branch_translations_search', 'branch_name_translations', ['search_vector'], unique=False, postgresql_using='gin')
    op.create_table('branch_address_history',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('address_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('dek_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('address_line1', sa.Text(), nullable=False),
    sa.Column('address_line2', sa.Text(), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=False),
    sa.Column('state_province', sa.String(length=100), nullable=True),
    sa.Column('country_code', sa.String(length=2), nullable=False),
    sa.Column('postal_code', sa.String(length=20), nullable=True),
    sa.Column('formatted_address', sa.Text(), nullable=True),
    sa.Column('change_reason', sa.Text(), nullable=True),
    sa.Column('valid_from', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('valid_to', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('changed_by', sa.UUID(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("address_line1 LIKE 'enc:%'", name='chk_hist_address_line1_encrypted'),
    sa.CheckConstraint('valid_to IS NULL OR valid_to > valid_from', name='chk_valid_range_nonempty'),
    sa.ForeignKeyConstraint(['changed_by'], ['gym_owners.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('branch_geocode_attempts',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('address_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('geocode_provider', sa.String(length=20), nullable=False),
    sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('attempted_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('succeeded', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.ForeignKeyConstraint(['address_id'], ['organization_addresses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('branch_geolocation_state',
    sa.Column('address_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('coordinates', sa.String(length=255), nullable=True),
    sa.Column('last_known_good_coordinates', sa.String(length=255), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('validation_status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('geocode_version', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('geocode_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_geocode_attempt_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('next_retry_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('geocoded_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('geocode_provider', sa.String(length=20), nullable=True),
    sa.CheckConstraint("validation_status IN ('pending', 'queued', 'success', 'failed', 'skipped')", name='chk_geocode_status'),
    sa.ForeignKeyConstraint(['address_id'], ['organization_addresses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('address_id')
    )
    op.alter_column('allowed_branch_transitions', 'from_status',
               existing_type=sa.TEXT(),
               type_=sa.String(),
               existing_nullable=False)
    op.alter_column('allowed_branch_transitions', 'to_status',
               existing_type=sa.TEXT(),
               type_=sa.String(),
               existing_nullable=False)
    op.execute("DROP INDEX IF EXISTS idx_places_cache_expires;")
    op.alter_column('member_addresses', 'address_type',
               existing_type=sa.VARCHAR(length=50),
               type_=sa.Enum('registered', 'operational', 'billing', name='addresstype', native_enum=False),
               existing_nullable=False,
               existing_server_default=sa.text("'operational'::character varying"))
    op.execute("DROP INDEX IF EXISTS uq_member_primary_address;")
    op.execute("ALTER TABLE org_branches DROP COLUMN IF EXISTS search_normalized_name CASCADE;")
    op.add_column('org_branches', sa.Column('search_normalized_name', sa.String(), sa.Computed("lower(regexp_replace(branch_name, '\\s+', ' ', 'g'))", persisted=True), nullable=True))
    op.alter_column('org_branches', 'internal_slug',
               existing_type=sa.VARCHAR(length=32),
               type_=postgresql.CITEXT(),
               existing_nullable=False)
    op.execute("DROP INDEX IF EXISTS ix_org_branches_org_id;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_name_trgm;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_normalized;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_org_id_v2;")
    op.create_index('ix_org_branches_name_trgm', 'org_branches', ['branch_name'], unique=False, postgresql_using='gin', postgresql_ops={'branch_name': 'gin_trgm_ops'})
    op.create_index('ix_org_branches_normalized', 'org_branches', ['search_normalized_name'], unique=False, postgresql_using='gin', postgresql_ops={'search_normalized_name': 'gin_trgm_ops'})
    op.create_index('ix_org_branches_org_id_v2', 'org_branches', ['org_id'], unique=False)

    # These columns are new in 00f.  Collision means predecessor drift and must
    # block rather than be erased with DROP COLUMN ... CASCADE.
    op.execute(r"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'organization_addresses'
                  AND column_name IN (
                      'branch_id', 'dek_version', 'allow_search_indexing',
                      '_reencryption_in_progress', 'deleted_by'
                  )
            ) THEN
                RAISE EXCEPTION
                    '00f predecessor unexpectedly already contains a 00f-owned organization_addresses column';
            END IF;
        END
        $$;
    """)
    op.add_column('organization_addresses', sa.Column('branch_id', sa.UUID(), nullable=True))
    op.add_column('organization_addresses', sa.Column('dek_version', sa.Integer(), server_default=sa.text('1'), nullable=False))
    op.add_column('organization_addresses', sa.Column('allow_search_indexing', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False))
    op.add_column('organization_addresses', sa.Column('_reencryption_in_progress', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False))
    op.add_column('organization_addresses', sa.Column('deleted_by', sa.UUID(), nullable=True))

    _backfill_legacy_addresses()
    op.alter_column(
        'organization_addresses',
        'branch_id',
        existing_type=sa.UUID(),
        nullable=False,
    )

    op.alter_column('organization_addresses', 'address_type',
               existing_type=sa.VARCHAR(length=50),
               type_=sa.String(length=20),
               existing_nullable=False,
               existing_server_default=sa.text("'physical'::character varying"))
    op.alter_column('organization_addresses', 'state_province',
               existing_type=sa.VARCHAR(length=100),
               nullable=True)
    op.alter_column('organization_addresses', 'postal_code',
               existing_type=sa.VARCHAR(length=15),
               type_=sa.String(length=20),
               existing_nullable=True)
    op.alter_column('organization_addresses', 'google_place_id',
               existing_type=sa.VARCHAR(length=300),
               type_=sa.Text(),
               existing_nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_org_addresses_city;")
    op.execute("DROP INDEX IF EXISTS idx_org_addresses_country_state;")
    op.execute("DROP INDEX IF EXISTS idx_org_addresses_lat_lng;")
    op.execute("DROP INDEX IF EXISTS idx_org_addresses_next_retry;")
    op.execute("DROP INDEX IF EXISTS idx_org_addresses_verification_status;")
    op.execute("DROP INDEX IF EXISTS uq_org_addresses_place_id;")
    op.execute("DROP INDEX IF EXISTS uq_org_primary_address;")
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS organization_addresses_org_id_fkey;")
    op.create_foreign_key(None, 'organization_addresses', 'gym_owners', ['deleted_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(None, 'organization_addresses', 'organizations', ['org_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(None, 'organization_addresses', 'org_branches', ['branch_id'], ['id'], ondelete='RESTRICT')

    # Fields represented durably by branch_geolocation_state / branch state can
    # now be removed.  Legacy fields with no durable replacement (label,
    # effective window, embed flag, verification error and the raw generic
    # source fields) are intentionally retained rather than silently discarded.
    op.execute("ALTER TABLE organization_addresses DROP COLUMN verified_at;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_updated_at;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN longitude;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_retry_count;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_next_retry_at;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN coordinates;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN is_primary;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_verification_source;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_last_verified_at;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN geocoding_failed;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN latitude;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN is_verified;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN maps_verification_status;")
    op.execute("ALTER TABLE organization_asset_audit DROP CONSTRAINT IF EXISTS organization_asset_audit_changed_by_fkey;")
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS organizations_cover_updated_by_fkey;")
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS organizations_logo_updated_by_fkey;")

    # Recreate the view dropped earlier.
    op.execute('''
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug, b.timezone, b.currency_code, b.region_code, b.country_code, b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public, s.version, s.updated_at AS state_updated_at
        FROM org_branches b JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
    ''')
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = true);")

    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_addr_history_open_window ON branch_address_history(address_id) WHERE valid_to IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_physical_per_branch ON organization_addresses(branch_id) WHERE address_type = 'physical' AND deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_google_place_id_per_org ON organization_addresses(org_id, google_place_id) WHERE google_place_id IS NOT NULL AND deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_slug_per_org_ci ON org_branches(org_id, internal_slug);")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_name_per_org ON org_branches(org_id, lower(branch_name));")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_default_translation_per_branch ON branch_name_translations(branch_id) WHERE is_default = TRUE;")

    op.execute("ALTER TABLE organization_addresses FORCE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_select ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_insert ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_update ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_delete ON organization_addresses;")
    op.execute("CREATE POLICY tenant_isolation_addr_select ON organization_addresses FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")
    op.execute("CREATE POLICY tenant_isolation_addr_insert ON organization_addresses FOR INSERT WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")
    op.execute("CREATE POLICY tenant_isolation_addr_update ON organization_addresses FOR UPDATE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")
    op.execute("CREATE POLICY tenant_isolation_addr_delete ON organization_addresses FOR DELETE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")
    op.execute("REVOKE ALL ON organization_addresses FROM public;")
    op.execute("GRANT INSERT, UPDATE ON organization_addresses TO branch_admin;")

    op.execute("ALTER TABLE branch_geocode_attempts FORCE ROW LEVEL SECURITY;")
    op.execute("CREATE POLICY geocode_attempts_tenant_isolation ON branch_geocode_attempts USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")

    op.execute("ALTER TABLE address_change_outbox FORCE ROW LEVEL SECURITY;")
    op.execute("CREATE POLICY outbox_tenant_isolation ON address_change_outbox USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")

    op.execute("ALTER TABLE branch_address_history FORCE ROW LEVEL SECURITY;")
    op.execute("CREATE POLICY tenant_isolation_hist ON branch_address_history USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")

    op.execute("ALTER TABLE branch_address_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute("CREATE POLICY tenant_isolation_audit_select ON branch_address_audit_log FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);")

    op.execute('''
    CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
    BEGIN
      NEW.updated_at := clock_timestamp();
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    ''')
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON organization_addresses;")
    op.execute("CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    op.execute('''
    CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'audit logs are immutable'; END;
    $$ LANGUAGE plpgsql;
    ''')
    op.execute("DROP TRIGGER IF EXISTS trg_immutable_audit ON branch_address_audit_log;")
    op.execute("CREATE TRIGGER trg_immutable_audit BEFORE UPDATE OR DELETE ON branch_address_audit_log FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();")

    op.execute('''
    CREATE OR REPLACE FUNCTION snapshot_address_on_insert() RETURNS trigger AS $$
    BEGIN
      IF current_setting('app.skip_history_snapshot', true) = 'true' THEN
        RETURN NEW;
      END IF;

      INSERT INTO branch_address_history
        (address_id, org_id, dek_version, address_line1, address_line2, city, state_province, country_code, postal_code, formatted_address, valid_from, changed_by)
      VALUES
        (NEW.id, NEW.org_id, NEW.dek_version, NEW.address_line1, NEW.address_line2, NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code, NEW.formatted_address, clock_timestamp(), NULLIF(current_setting('app.current_user_id', true), '')::UUID);
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    ''')
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_on_insert ON organization_addresses;")
    op.execute("CREATE TRIGGER trg_snapshot_address_on_insert AFTER INSERT ON organization_addresses FOR EACH ROW EXECUTE FUNCTION snapshot_address_on_insert();")

    op.execute('''
    CREATE OR REPLACE FUNCTION snapshot_address_on_change() RETURNS trigger AS $$
    DECLARE
      v_now TIMESTAMPTZ := clock_timestamp();
    BEGIN
      IF NEW._reencryption_in_progress = TRUE THEN
        IF ROW(OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code) IS NOT DISTINCT FROM ROW(NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN
          NEW._reencryption_in_progress := FALSE;
          RETURN NEW;
        END IF;
        RAISE EXCEPTION 'plaintext fields mutated during KMS re-encryption pass: address_id=%', OLD.id;
      END IF;

      IF ROW(OLD.address_line1, OLD.address_line2, OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code) IS DISTINCT FROM
         ROW(NEW.address_line1, NEW.address_line2, NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN

        UPDATE branch_address_history SET valid_to = v_now WHERE address_id = OLD.id AND valid_to IS NULL;

        INSERT INTO branch_address_history
          (address_id, org_id, dek_version, address_line1, address_line2, city, state_province, country_code, postal_code, formatted_address, valid_from, changed_by)
        VALUES
          (OLD.id, OLD.org_id, OLD.dek_version, OLD.address_line1, OLD.address_line2, OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code, OLD.formatted_address, v_now, NULLIF(current_setting('app.current_user_id', true), '')::UUID);

        INSERT INTO branch_address_audit_log(event_id, address_id, org_id, dek_version, old_address, new_address, changed_by, ip_address, user_agent, request_id)
        VALUES (
          gen_random_uuid(),
          OLD.id, OLD.org_id, OLD.dek_version,
          jsonb_build_object('city', OLD.city, 'state', OLD.state_province, 'country_code', OLD.country_code, 'postal_code', OLD.postal_code, 'dek_version', OLD.dek_version, 'address_line1_hash', encode(sha256(OLD.address_line1::bytea), 'hex')),
          jsonb_build_object('city', NEW.city, 'state', NEW.state_province, 'country_code', NEW.country_code, 'postal_code', NEW.postal_code, 'dek_version', NEW.dek_version, 'address_line1_hash', encode(sha256(NEW.address_line1::bytea), 'hex')),
          NULLIF(current_setting('app.current_user_id', true), '')::UUID,
          NULLIF(current_setting('app.ip_address', true), '')::INET,
          NULLIF(current_setting('app.user_agent', true), ''),
          NULLIF(current_setting('app.request_id', true), '')::UUID
        );

        INSERT INTO address_change_outbox (address_id, org_id, event_type, payload)
        VALUES (NEW.id, NEW.org_id, 'address_updated', jsonb_build_object('address_id', NEW.id, 'timestamp', v_now));
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    ''')
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_history ON organization_addresses;")
    op.execute("CREATE TRIGGER trg_snapshot_address_history BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION snapshot_address_on_change();")

    op.execute('''
    CREATE OR REPLACE VIEW v_public_branch_addresses WITH (security_barrier = true) AS
    SELECT
        a.id,
        a.city,
        a.country_code,
        a.google_place_id,
        a.allow_search_indexing,
        g.coordinates
    FROM organization_addresses a
    JOIN branch_geolocation_state g ON a.id = g.address_id
    WHERE a.deleted_at IS NULL
      AND a.allow_search_indexing = TRUE
      AND (g.validation_status = 'success' OR g.last_known_good_coordinates IS NOT NULL)
      AND a.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID;
    ''')
    op.execute("ALTER VIEW v_public_branch_addresses SET (security_invoker = true);")
    op.execute("GRANT SELECT ON v_public_branch_addresses TO branch_viewer;")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW IF EXISTS v_public_branch_addresses;")
    op.execute("DROP VIEW IF EXISTS v_active_org_branches;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_select ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_insert ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_update ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_delete ON organization_addresses;")
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON organization_addresses;")
    op.execute("DROP TRIGGER IF EXISTS trg_immutable_audit ON branch_address_audit_log;")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_on_insert ON organization_addresses;")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_history ON organization_addresses;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS snapshot_address_on_insert() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS snapshot_address_on_change() CASCADE;")
    op.create_foreign_key(op.f('organizations_logo_updated_by_fkey'), 'organizations', 'gym_owners', ['logo_updated_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('organizations_cover_updated_by_fkey'), 'organizations', 'gym_owners', ['cover_updated_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('organization_asset_audit_changed_by_fkey'), 'organization_asset_audit', 'gym_owners', ['changed_by'], ['id'], ondelete='SET NULL')
    op.add_column('organization_addresses', sa.Column('maps_verification_status', sa.VARCHAR(length=30), server_default=sa.text("'pending'::character varying"), autoincrement=False, nullable=False))
    op.add_column('organization_addresses', sa.Column('is_verified', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
    op.add_column('organization_addresses', sa.Column('latitude', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('geocoding_failed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
    op.add_column('organization_addresses', sa.Column('maps_last_verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('maps_verification_source', sa.VARCHAR(length=50), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('is_primary', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
    op.execute("""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_extension AS extension_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = extension_data.extowner
        WHERE extension_data.extname = 'postgis'
          AND owner_role.rolname = 'postgres'
      ) THEN
        RAISE EXCEPTION
          '00f downgrade requires infrastructure-owned PostGIS before restoring the 371b predecessor spatial contract';
      END IF;
    END
    $$;
    """)
    op.execute("ALTER TABLE public.organization_addresses ADD COLUMN coordinates geography(POINT,4326);")
    op.execute("CREATE INDEX idx_organization_addresses_coordinates ON public.organization_addresses USING gist (coordinates);")
    op.add_column('organization_addresses', sa.Column('maps_next_retry_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('maps_retry_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False))
    op.add_column('organization_addresses', sa.Column('longitude', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('maps_updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('organization_addresses', sa.Column('verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS organization_addresses_deleted_by_fkey;")
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS organization_addresses_org_id_fkey;")
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS organization_addresses_branch_id_fkey;")
    op.create_foreign_key(op.f('organization_addresses_org_id_fkey'), 'organization_addresses', 'organizations', ['org_id'], ['id'], ondelete='CASCADE')
    op.create_index(op.f('uq_org_primary_address'), 'organization_addresses', ['org_id'], unique=True, postgresql_where='((is_primary = true) AND (deleted_at IS NULL))')
    op.create_index(op.f('uq_org_addresses_place_id'), 'organization_addresses', ['org_id', 'google_place_id'], unique=True, postgresql_where='((google_place_id IS NOT NULL) AND (deleted_at IS NULL))')
    op.create_index(op.f('idx_org_addresses_verification_status'), 'organization_addresses', ['maps_verification_status'], unique=False, postgresql_where="((maps_verification_status)::text = ANY ((ARRAY['pending'::character varying, 'stale'::character varying])::text[]))")
    op.create_index(op.f('idx_org_addresses_next_retry'), 'organization_addresses', ['maps_next_retry_at'], unique=False, postgresql_where='((maps_next_retry_at IS NOT NULL) AND (deleted_at IS NULL))')
    op.create_index(op.f('idx_org_addresses_lat_lng'), 'organization_addresses', ['latitude', 'longitude'], unique=False, postgresql_where='((latitude IS NOT NULL) AND (longitude IS NOT NULL) AND (deleted_at IS NULL))')
    op.create_index(op.f('idx_org_addresses_country_state'), 'organization_addresses', ['country_code', 'state_province'], unique=False, postgresql_where='(deleted_at IS NULL)')
    op.create_index(op.f('idx_org_addresses_city'), 'organization_addresses', ['city'], unique=False, postgresql_where='(deleted_at IS NULL)')
    op.alter_column('organization_addresses', 'google_place_id',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=300),
               existing_nullable=True)
    op.alter_column('organization_addresses', 'postal_code',
               existing_type=sa.String(length=20),
               type_=sa.VARCHAR(length=15),
               existing_nullable=True)
    op.alter_column('organization_addresses', 'state_province',
               existing_type=sa.VARCHAR(length=100),
               nullable=False)
    op.alter_column('organization_addresses', 'address_type',
               existing_type=sa.String(length=20),
               type_=sa.VARCHAR(length=50),
               existing_nullable=False,
               existing_server_default=sa.text("'operational'::character varying"))
    op.execute("ALTER TABLE organization_addresses DROP COLUMN IF EXISTS deleted_by CASCADE;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN IF EXISTS _reencryption_in_progress CASCADE;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN IF EXISTS allow_search_indexing CASCADE;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN IF EXISTS dek_version CASCADE;")
    op.execute("ALTER TABLE organization_addresses DROP COLUMN IF EXISTS branch_id CASCADE;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_org_id_v2;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_normalized;")
    op.execute("DROP INDEX IF EXISTS ix_org_branches_name_trgm;")
    op.create_index(op.f('ix_org_branches_org_id'), 'org_branches', ['org_id'], unique=False, postgresql_include=['branch_name', 'branch_code', 'created_at'])
    op.alter_column('org_branches', 'internal_slug',
               existing_type=postgresql.CITEXT(),
               type_=sa.VARCHAR(length=32),
               existing_nullable=False)
    op.execute("ALTER TABLE org_branches DROP COLUMN IF EXISTS search_normalized_name CASCADE;")
    op.create_index(op.f('uq_member_primary_address'), 'member_addresses', ['member_id'], unique=True, postgresql_where='((is_primary = true) AND (deleted_at IS NULL))')
    op.alter_column('member_addresses', 'address_type',
               existing_type=sa.Enum('registered', 'operational', 'billing', name='addresstype', native_enum=False),
               type_=sa.VARCHAR(length=50),
               existing_nullable=False,
               existing_server_default=sa.text("'operational'::character varying"))
    op.create_index(op.f('idx_places_cache_expires'), 'google_places_cache', ['expires_at'], unique=False)
    op.alter_column('allowed_branch_transitions', 'to_status',
               existing_type=sa.String(),
               type_=sa.TEXT(),
               existing_nullable=False)
    op.alter_column('allowed_branch_transitions', 'from_status',
               existing_type=sa.String(),
               type_=sa.TEXT(),
               existing_nullable=False)
    op.drop_table('branch_geolocation_state')
    op.drop_table('branch_geocode_attempts')
    op.drop_table('branch_address_history')
    op.execute("DROP INDEX IF EXISTS ix_branch_translations_search;")
    op.drop_table('branch_name_translations')
    op.drop_table('branch_address_audit_log')
    op.drop_table('address_change_outbox')
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS chk_address_type;")
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT IF EXISTS ck_org_address_type;")
    op.execute("UPDATE organization_addresses SET address_type = 'operational' WHERE address_type = 'physical';")
    op.execute("ALTER TABLE organization_addresses ADD CONSTRAINT ck_org_address_type CHECK (address_type IN ('registered', 'operational', 'billing'));")
    op.execute('''
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug, b.timezone, b.currency_code, b.region_code, b.country_code, b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public, s.version, s.updated_at AS state_updated_at
        FROM org_branches b JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
    ''')
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = true);")
