"""Add the branch/address compatibility architecture without destroying 0009 state.

Revision ID: 00f277c748ea
Revises: 0009_view_security_invoker
Create Date: 2026-05-22 18:53:22.258447

The revision is deliberately an expand migration.  Revision 0009-owned address
columns, constraints and indexes stay present so a rollback can be lossless and
older application binaries remain representable during the compatibility
window.  A later, explicitly gated contract migration may remove predecessor
storage only after dual-write/read compatibility has been proven in production.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "00f277c748ea"
down_revision: Union[str, Sequence[str], None] = "0009_view_security_invoker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BRANCH_ORG_FK = "fk_org_addresses_branch_org"
_DELETED_BY_FK = "fk_org_addresses_deleted_by"
_SYNTHETIC_MARKER = "migration_00f_legacy_backfill"

_TARGET_RELATIONS = (
    "address_change_outbox",
    "branch_address_audit_log",
    "branch_name_translations",
    "branch_address_history",
    "branch_geocode_attempts",
    "branch_geolocation_state",
)

_REQUIRED_PREDECESSOR_COLUMNS = (
    "id",
    "org_id",
    "address_type",
    "address_line1",
    "address_line2",
    "city",
    "state_province",
    "postal_code",
    "country_code",
    "label",
    "is_verified",
    "verified_at",
    "verification_source",
    "coordinates",
    "coordinates_source",
    "is_exact_location_visible",
    "formatted_address",
    "deleted_at",
    "created_at",
    "updated_at",
    "is_primary",
    "geocoding_failed",
    "effective_from",
    "effective_until",
    "google_place_id",
    "latitude",
    "longitude",
    "maps_embed_allowed",
    "maps_verification_status",
    "maps_last_verified_at",
    "maps_verification_error",
    "maps_verification_source",
    "maps_updated_at",
    "maps_next_retry_at",
    "maps_retry_count",
)

_REQUIRED_PREDECESSOR_CONSTRAINTS = (
    "ck_org_address_type",
    "chk_latitude",
    "chk_longitude",
    "chk_maps_retry_count",
    "chk_maps_verification_error",
    "chk_maps_verification_source",
    "chk_maps_verification_status",
    "chk_verified_maps_have_coordinates",
    "organization_addresses_org_id_fkey",
    "organization_addresses_pkey",
)

_REQUIRED_PREDECESSOR_INDEXES = (
    "idx_org_addresses_city",
    "idx_org_addresses_country_state",
    "idx_org_addresses_lat_lng",
    "idx_org_addresses_next_retry",
    "idx_org_addresses_verification_status",
    "idx_organization_addresses_coordinates",
    "uq_org_addresses_place_id",
    "uq_org_primary_address",
)


def _require_migration_owner() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF current_user <> 'migration_owner' THEN
                RAISE EXCEPTION
                    '00f migration must execute as migration_owner, got %',
                    current_user;
            END IF;
        END
        $$;
        """
    )


def _preflight_upgrade() -> None:
    """Validate the exact predecessor and fail before any mutation."""
    _require_migration_owner()

    op.execute(
        r"""
        DO $$
        DECLARE
            item text;
            collision text;
            relation_row record;
        BEGIN
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
                    '00f target relation public.% already exists; refusing adoption',
                    collision;
            END IF;

            FOREACH item IN ARRAY ARRAY[
                'id', 'org_id', 'address_type', 'address_line1', 'address_line2',
                'city', 'state_province', 'postal_code', 'country_code', 'label',
                'is_verified', 'verified_at', 'verification_source', 'coordinates',
                'coordinates_source', 'is_exact_location_visible',
                'formatted_address', 'deleted_at', 'created_at', 'updated_at',
                'is_primary', 'geocoding_failed', 'effective_from',
                'effective_until', 'google_place_id', 'latitude', 'longitude',
                'maps_embed_allowed', 'maps_verification_status',
                'maps_last_verified_at', 'maps_verification_error',
                'maps_verification_source', 'maps_updated_at',
                'maps_next_retry_at', 'maps_retry_count'
            ] LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'organization_addresses'
                      AND column_name = item
                ) THEN
                    RAISE EXCEPTION
                        '00f predecessor column public.organization_addresses.% is missing',
                        item;
                END IF;
            END LOOP;

            FOREACH item IN ARRAY ARRAY[
                'ck_org_address_type',
                'chk_latitude',
                'chk_longitude',
                'chk_maps_retry_count',
                'chk_maps_verification_error',
                'chk_maps_verification_source',
                'chk_maps_verification_status',
                'chk_verified_maps_have_coordinates',
                'organization_addresses_org_id_fkey',
                'organization_addresses_pkey'
            ] LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint AS constraint_data
                    JOIN pg_catalog.pg_class AS relation_data
                      ON relation_data.oid = constraint_data.conrelid
                    JOIN pg_catalog.pg_namespace AS namespace_data
                      ON namespace_data.oid = relation_data.relnamespace
                    WHERE namespace_data.nspname = 'public'
                      AND relation_data.relname = 'organization_addresses'
                      AND constraint_data.conname = item
                ) THEN
                    RAISE EXCEPTION
                        '00f predecessor constraint public.organization_addresses.% is missing',
                        item;
                END IF;
            END LOOP;

            FOREACH item IN ARRAY ARRAY[
                'idx_org_addresses_city',
                'idx_org_addresses_country_state',
                'idx_org_addresses_lat_lng',
                'idx_org_addresses_next_retry',
                'idx_org_addresses_verification_status',
                'idx_organization_addresses_coordinates',
                'uq_org_addresses_place_id',
                'uq_org_primary_address'
            ] LOOP
                IF to_regclass('public.' || item) IS NULL THEN
                    RAISE EXCEPTION '00f predecessor index public.% is missing', item;
                END IF;
            END LOOP;

            SELECT
                relation_data.relrowsecurity,
                relation_data.relforcerowsecurity,
                pg_catalog.pg_get_userbyid(relation_data.relowner) AS owner_name
            INTO relation_row
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = 'organization_addresses';

            IF NOT FOUND
               OR relation_row.owner_name <> 'migration_owner'
               OR relation_row.relrowsecurity
               OR relation_row.relforcerowsecurity THEN
                RAISE EXCEPTION
                    '00f predecessor organization_addresses security/owner drift: %',
                    row_to_json(relation_row);
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
                    '00f cannot preserve a geocode source longer than 20 characters';
            END IF;

            FOREACH item IN ARRAY ARRAY[
                'btree_gist', 'citext', 'pg_trgm', 'postgis'
            ] LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_extension AS extension_data
                    JOIN pg_catalog.pg_roles AS owner_role
                      ON owner_role.oid = extension_data.extowner
                    WHERE extension_data.extname = item
                      AND owner_role.rolname = 'postgres'
                ) THEN
                    RAISE EXCEPTION
                        '00f requires infrastructure-owned extension %', item;
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM (VALUES
                    ('branch_admin'), ('branch_viewer'), ('ops_support')
                ) AS required(role_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles AS role_data
                    WHERE role_data.rolname = required.role_name
                      AND NOT role_data.rolsuper
                      AND NOT role_data.rolbypassrls
                      AND NOT role_data.rolcanlogin
                      AND NOT role_data.rolinherit
                )
            ) THEN
                RAISE EXCEPTION
                    'Managed cluster roles do not match the approved bootstrap contract';
            END IF;
        END
        $$;
        """
    )


def _backfill_legacy_addresses() -> None:
    """Create deterministic branch mappings tenant-by-tenant under FORCE RLS."""
    op.execute(
        r"""
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
                                org_row.id, address_row.id;
                        END IF;

                        INSERT INTO public.org_branches (
                            id, org_id, branch_name, branch_code, internal_slug,
                            country_code, address_id, branch_metadata
                        ) VALUES (
                            new_branch_id,
                            org_row.id,
                            CASE
                                WHEN ordinal = 1 THEN left(
                                    COALESCE(
                                        NULLIF(address_row.label, ''),
                                        NULLIF(org_row.name, ''),
                                        'Main Branch'
                                    ), 120
                                )
                                ELSE 'Legacy Location ' ||
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
                            branch_id, org_id, branch_status, is_primary,
                            is_active, is_public, search_epoch_ulid
                        ) VALUES (
                            new_branch_id, org_row.id, 'active', ordinal = 1,
                            true, true, '00000000000000000000000000'
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
                            id, org_id, branch_name, branch_code, internal_slug,
                            country_code, address_id, branch_metadata
                        )
                        SELECT
                            new_branch_id,
                            org_row.id,
                            left(
                                COALESCE(
                                    NULLIF(address_data.label, ''),
                                    NULLIF(org_row.name, ''),
                                    'Main Branch'
                                ), 120
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
                            branch_id, org_id, branch_status, is_primary,
                            is_active, is_public, search_epoch_ulid
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
                            '00f cannot safely map % unmatched active physical addresses across existing branches for organization %',
                            unmatched_physical_count, org_row.id;
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
                     AND branch_data.org_id = address_data.org_id
                    WHERE address_data.org_id = org_row.id
                      AND (
                           address_data.branch_id IS NULL
                        OR branch_data.id IS NULL
                      )
                ) THEN
                    RAISE EXCEPTION
                        '00f branch backfill left a null, missing, or cross-tenant mapping for organization %',
                        org_row.id;
                END IF;
            END LOOP;

            PERFORM pg_catalog.set_config(
                'app.current_org_id',
                '00000000-0000-0000-0000-000000000000',
                true
            );

            IF EXISTS (
                SELECT 1
                FROM public.organization_addresses
                WHERE branch_id IS NULL
            ) THEN
                RAISE EXCEPTION '00f branch backfill left a null branch assignment';
            END IF;
        END
        $$;
        """
    )

    op.execute(
        r"""
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
            address_data.id,
            address_data.org_id,
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
            END,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (
                        address_data.latitude IS NOT NULL
                        AND address_data.longitude IS NOT NULL
                    )
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN
                    CASE
                        WHEN address_data.coordinates IS NOT NULL THEN
                            ST_AsText(address_data.coordinates::geometry)
                        ELSE format(
                            'POINT(%s %s)',
                            address_data.longitude,
                            address_data.latitude
                        )
                    END
                ELSE NULL
            END,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (
                        address_data.latitude IS NOT NULL
                        AND address_data.longitude IS NOT NULL
                    )
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN 'success'
                WHEN address_data.geocoding_failed
                  OR address_data.maps_verification_status = 'failed' THEN 'failed'
                WHEN address_data.maps_verification_status = 'disabled' THEN 'skipped'
                ELSE 'pending'
            END,
            address_data.maps_retry_count,
            address_data.maps_updated_at,
            address_data.maps_next_retry_at,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (
                        address_data.latitude IS NOT NULL
                        AND address_data.longitude IS NOT NULL
                    )
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN COALESCE(
                    address_data.maps_last_verified_at,
                    address_data.verified_at
                )
                ELSE NULL
            END,
            NULLIF(
                COALESCE(
                    NULLIF(address_data.maps_verification_source, ''),
                    NULLIF(address_data.verification_source, ''),
                    NULLIF(address_data.coordinates_source, '')
                ),
                ''
            )
        FROM public.organization_addresses AS address_data;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF (
                SELECT count(*) FROM public.branch_geolocation_state
            ) <> (
                SELECT count(*) FROM public.organization_addresses
            ) THEN
                RAISE EXCEPTION
                    '00f geolocation backfill did not create one state row per address';
            END IF;
        END
        $$;
        """
    )


def _preflight_downgrade() -> None:
    """Refuse rollback whenever 00f-only state cannot be represented by 0009."""
    _require_migration_owner()

    op.execute(
        r"""
        DO $$
        DECLARE
            relation_name text;
            expected_coordinates text;
            expected_last_good text;
            expected_status text;
            expected_geocoded_at timestamptz;
            expected_provider text;
            state_row record;
            address_row record;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.organization_addresses
                WHERE address_type = 'mailing'
                   OR deleted_by IS NOT NULL
                   OR dek_version <> 1
                   OR allow_search_indexing IS DISTINCT FROM true
                   OR _reencryption_in_progress IS DISTINCT FROM false
            ) THEN
                RAISE EXCEPTION
                    '00f downgrade would lose address state that 0009 cannot represent';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.org_branches
                WHERE length(internal_slug::text) > 32
            ) THEN
                RAISE EXCEPTION
                    '00f downgrade cannot restore VARCHAR(32) internal_slug safely';
            END IF;

            FOREACH relation_name IN ARRAY ARRAY[
                'address_change_outbox',
                'branch_address_audit_log',
                'branch_name_translations',
                'branch_address_history',
                'branch_geocode_attempts'
            ] LOOP
                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)',
                    relation_name
                ) INTO expected_coordinates;

                IF expected_coordinates::boolean THEN
                    RAISE EXCEPTION
                        '00f downgrade would discard populated 00f-only relation public.%',
                        relation_name;
                END IF;
            END LOOP;

            FOR address_row IN
                SELECT *
                FROM public.organization_addresses
                ORDER BY id
            LOOP
                expected_coordinates := CASE
                    WHEN address_row.coordinates IS NOT NULL THEN
                        ST_AsText(address_row.coordinates::geometry)
                    WHEN address_row.latitude IS NOT NULL
                     AND address_row.longitude IS NOT NULL THEN
                        format(
                            'POINT(%s %s)',
                            address_row.longitude,
                            address_row.latitude
                        )
                    ELSE NULL
                END;

                expected_last_good := CASE
                    WHEN expected_coordinates IS NOT NULL
                     AND (
                         address_row.is_verified
                         OR address_row.maps_verification_status = 'verified'
                     ) THEN expected_coordinates
                    ELSE NULL
                END;

                expected_status := CASE
                    WHEN expected_coordinates IS NOT NULL
                     AND (
                         address_row.is_verified
                         OR address_row.maps_verification_status = 'verified'
                     ) THEN 'success'
                    WHEN address_row.geocoding_failed
                      OR address_row.maps_verification_status = 'failed' THEN 'failed'
                    WHEN address_row.maps_verification_status = 'disabled' THEN 'skipped'
                    ELSE 'pending'
                END;

                expected_geocoded_at := CASE
                    WHEN expected_coordinates IS NOT NULL
                     AND (
                         address_row.is_verified
                         OR address_row.maps_verification_status = 'verified'
                     ) THEN COALESCE(
                         address_row.maps_last_verified_at,
                         address_row.verified_at
                     )
                    ELSE NULL
                END;

                expected_provider := NULLIF(
                    COALESCE(
                        NULLIF(address_row.maps_verification_source, ''),
                        NULLIF(address_row.verification_source, ''),
                        NULLIF(address_row.coordinates_source, '')
                    ),
                    ''
                );

                SELECT *
                INTO state_row
                FROM public.branch_geolocation_state
                WHERE address_id = address_row.id;

                IF NOT FOUND
                   OR state_row.org_id <> address_row.org_id
                   OR state_row.coordinates IS DISTINCT FROM expected_coordinates
                   OR state_row.last_known_good_coordinates IS DISTINCT FROM expected_last_good
                   OR state_row.timezone IS NOT NULL
                   OR state_row.validation_status IS DISTINCT FROM expected_status
                   OR state_row.geocode_version <> 1
                   OR state_row.geocode_attempts <> address_row.maps_retry_count
                   OR state_row.last_geocode_attempt_at IS DISTINCT FROM address_row.maps_updated_at
                   OR state_row.next_retry_at IS DISTINCT FROM address_row.maps_next_retry_at
                   OR state_row.geocoded_at IS DISTINCT FROM expected_geocoded_at
                   OR state_row.geocode_provider IS DISTINCT FROM expected_provider THEN
                    RAISE EXCEPTION
                        '00f downgrade would lose diverged geolocation state for address %',
                        address_row.id;
                END IF;
            END LOOP;

            IF (
                SELECT count(*) FROM public.branch_geolocation_state
            ) <> (
                SELECT count(*) FROM public.organization_addresses
            ) THEN
                RAISE EXCEPTION
                    '00f downgrade found geolocation rows without predecessor addresses';
            END IF;
        END
        $$;
        """
    )


def _restore_address_predecessor_security() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_select ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_insert ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_update ON organization_addresses;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_addr_delete ON organization_addresses;")
    op.execute("ALTER TABLE organization_addresses NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE organization_addresses DISABLE ROW LEVEL SECURITY;")
    op.execute("REVOKE INSERT, UPDATE ON organization_addresses FROM branch_admin;")


def _drop_synthesized_branches() -> None:
    """Remove only untouched branches explicitly owned by this migration."""
    op.execute(
        r"""
        DO $$
        DECLARE
            org_row record;
        BEGIN
            FOR org_row IN
                SELECT DISTINCT branch_data.org_id
                FROM public.org_branches AS branch_data
                WHERE branch_data.branch_metadata->>'migration_00f_legacy_backfill' = 'true'
                ORDER BY branch_data.org_id
            LOOP
                PERFORM pg_catalog.set_config(
                    'app.current_org_id', org_row.org_id::text, true
                );

                DELETE FROM public.org_branch_state AS state_data
                USING public.org_branches AS branch_data
                WHERE state_data.branch_id = branch_data.id
                  AND state_data.org_id = branch_data.org_id
                  AND branch_data.org_id = org_row.org_id
                  AND branch_data.branch_metadata->>'migration_00f_legacy_backfill' = 'true';

                DELETE FROM public.org_branches AS branch_data
                WHERE branch_data.org_id = org_row.org_id
                  AND branch_data.branch_metadata->>'migration_00f_legacy_backfill' = 'true';
            END LOOP;

            PERFORM pg_catalog.set_config(
                'app.current_org_id',
                '00000000-0000-0000-0000-000000000000',
                true
            );
        END
        $$;
        """
    )


def upgrade() -> None:
    """Expand 0009 in place while preserving all predecessor address state."""
    _preflight_upgrade()

    # The business semantic rename is data-reversible.  Storage stays VARCHAR(50)
    # during this expand phase so the predecessor can be restored exactly.
    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT ck_org_address_type;")
    op.execute("UPDATE organization_addresses SET address_type = 'physical' WHERE address_type = 'operational';")
    op.execute("ALTER TABLE organization_addresses ALTER COLUMN address_type SET DEFAULT 'physical';")
    op.execute(
        "ALTER TABLE organization_addresses ADD CONSTRAINT chk_address_type "
        "CHECK (address_type IN ('physical', 'mailing', 'billing', 'registered'));"
    )

    # v_active_org_branches depends on internal_slug while its type is upgraded.
    op.execute("DROP VIEW v_active_org_branches;")

    op.create_table(
        "address_change_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("address_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("processed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "branch_address_audit_log",
        sa.Column("event_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("address_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("dek_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("old_address", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_address", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("changed_by", sa.UUID(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column(
            "changed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )

    op.create_table(
        "branch_name_translations",
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("branch_name", sa.String(length=120), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', branch_name)", persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["branch_id"], ["org_branches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("branch_id", "locale"),
    )
    op.create_index(
        "ix_branch_translations_search",
        "branch_name_translations",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "branch_address_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("address_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("dek_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("address_line1", sa.Text(), nullable=False),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("state_province", sa.String(length=100), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("formatted_address", sa.Text(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("valid_from", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("valid_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("changed_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("address_line1 LIKE 'enc:%'", name="chk_hist_address_line1_encrypted"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="chk_valid_range_nonempty"),
        sa.ForeignKeyConstraint(["changed_by"], ["gym_owners.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "branch_geocode_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("address_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("geocode_provider", sa.String(length=20), nullable=False),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "attempted_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("succeeded", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.ForeignKeyConstraint(["address_id"], ["organization_addresses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "branch_geolocation_state",
        sa.Column("address_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("coordinates", sa.String(length=255), nullable=True),
        sa.Column("last_known_good_coordinates", sa.String(length=255), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("validation_status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("geocode_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("geocode_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_geocode_attempt_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_retry_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("geocoded_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("geocode_provider", sa.String(length=20), nullable=True),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'queued', 'success', 'failed', 'skipped')",
            name="chk_geocode_status",
        ),
        sa.ForeignKeyConstraint(["address_id"], ["organization_addresses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("address_id"),
    )

    # Additive branch search capability; rename the predecessor org_id index
    # rather than keeping duplicate btree indexes for the same key.
    op.execute("ALTER INDEX ix_org_branches_org_id RENAME TO ix_org_branches_org_id_v2;")
    op.add_column(
        "org_branches",
        sa.Column(
            "search_normalized_name",
            sa.String(),
            sa.Computed("lower(regexp_replace(branch_name, '\\s+', ' ', 'g'))", persisted=True),
            nullable=True,
        ),
    )
    op.alter_column(
        "org_branches",
        "internal_slug",
        existing_type=sa.VARCHAR(length=32),
        type_=postgresql.CITEXT(),
        existing_nullable=False,
    )
    op.create_index(
        "ix_org_branches_name_trgm",
        "org_branches",
        ["branch_name"],
        postgresql_using="gin",
        postgresql_ops={"branch_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_org_branches_normalized",
        "org_branches",
        ["search_normalized_name"],
        postgresql_using="gin",
        postgresql_ops={"search_normalized_name": "gin_trgm_ops"},
    )

    # 00f-owned address columns are appended, never substituted for 0009 data.
    op.add_column("organization_addresses", sa.Column("branch_id", sa.UUID(), nullable=True))
    op.add_column(
        "organization_addresses",
        sa.Column("dek_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "organization_addresses",
        sa.Column("allow_search_indexing", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
    )
    op.add_column(
        "organization_addresses",
        sa.Column("_reencryption_in_progress", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
    )
    op.add_column("organization_addresses", sa.Column("deleted_by", sa.UUID(), nullable=True))

    # Install tenant integrity while branch_id is still NULL.  Backfill writes
    # are then checked by PostgreSQL RI triggers without disabling FORCE RLS.
    op.create_foreign_key(
        _BRANCH_ORG_FK,
        "organization_addresses",
        "org_branches",
        ["branch_id", "org_id"],
        ["id", "org_id"],
        source_schema="public",
        referent_schema="public",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        _DELETED_BY_FK,
        "organization_addresses",
        "gym_owners",
        ["deleted_by"],
        ["id"],
        source_schema="public",
        referent_schema="public",
        ondelete="SET NULL",
    )

    _backfill_legacy_addresses()

    op.alter_column(
        "organization_addresses",
        "branch_id",
        existing_type=sa.UUID(),
        nullable=False,
    )

    op.execute(
        """
        DO $$
        DECLARE
            fk_record record;
        BEGIN
            SELECT
                constraint_data.convalidated,
                pg_catalog.pg_get_constraintdef(constraint_data.oid, true) AS definition
            INTO fk_record
            FROM pg_catalog.pg_constraint AS constraint_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = constraint_data.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = 'organization_addresses'
              AND constraint_data.conname = 'fk_org_addresses_branch_org'
              AND constraint_data.contype = 'f';

            IF NOT FOUND
               OR NOT fk_record.convalidated
               OR fk_record.definition <>
                  'FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id) ON DELETE RESTRICT' THEN
                RAISE EXCEPTION
                    '00f tenant-composite address->branch FK contract drift: %',
                    row_to_json(fk_record);
            END IF;
        END
        $$;
        """
    )

    # 0009-owned geocoding columns remain authoritative compatibility storage.
    # The new state table is a lossless projection until a later contract phase.
    op.execute(
        r"""
        INSERT INTO public.branch_geolocation_state (
            address_id, org_id, coordinates, last_known_good_coordinates,
            validation_status, geocode_attempts, last_geocode_attempt_at,
            next_retry_at, geocoded_at, geocode_provider
        )
        SELECT
            address_data.id,
            address_data.org_id,
            CASE
                WHEN address_data.coordinates IS NOT NULL THEN
                    ST_AsText(address_data.coordinates::geometry)
                WHEN address_data.latitude IS NOT NULL
                 AND address_data.longitude IS NOT NULL THEN
                    format('POINT(%s %s)', address_data.longitude, address_data.latitude)
                ELSE NULL
            END,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (address_data.latitude IS NOT NULL AND address_data.longitude IS NOT NULL)
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN
                    CASE
                        WHEN address_data.coordinates IS NOT NULL THEN
                            ST_AsText(address_data.coordinates::geometry)
                        ELSE format('POINT(%s %s)', address_data.longitude, address_data.latitude)
                    END
                ELSE NULL
            END,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (address_data.latitude IS NOT NULL AND address_data.longitude IS NOT NULL)
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN 'success'
                WHEN address_data.geocoding_failed
                  OR address_data.maps_verification_status = 'failed' THEN 'failed'
                WHEN address_data.maps_verification_status = 'disabled' THEN 'skipped'
                ELSE 'pending'
            END,
            address_data.maps_retry_count,
            address_data.maps_updated_at,
            address_data.maps_next_retry_at,
            CASE
                WHEN (
                    address_data.coordinates IS NOT NULL
                    OR (address_data.latitude IS NOT NULL AND address_data.longitude IS NOT NULL)
                )
                AND (
                    address_data.is_verified
                    OR address_data.maps_verification_status = 'verified'
                ) THEN COALESCE(
                    address_data.maps_last_verified_at,
                    address_data.verified_at
                )
                ELSE NULL
            END,
            NULLIF(
                COALESCE(
                    NULLIF(address_data.maps_verification_source, ''),
                    NULLIF(address_data.verification_source, ''),
                    NULLIF(address_data.coordinates_source, '')
                ),
                ''
            )
        FROM public.organization_addresses AS address_data;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF (SELECT count(*) FROM public.branch_geolocation_state)
               <> (SELECT count(*) FROM public.organization_addresses) THEN
                RAISE EXCEPTION
                    '00f geolocation projection did not create one state row per address';
            END IF;
        END
        $$;
        """
    )

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_addr_history_open_window "
            "ON branch_address_history(address_id) WHERE valid_to IS NULL;"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_one_physical_per_branch "
            "ON organization_addresses(branch_id) "
            "WHERE address_type = 'physical' AND deleted_at IS NULL;"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_branch_slug_per_org_ci "
            "ON org_branches(org_id, internal_slug);"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_branch_name_per_org "
            "ON org_branches(org_id, lower(branch_name));"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY uq_one_default_translation_per_branch "
            "ON branch_name_translations(branch_id) WHERE is_default = TRUE;"
        )

    # Explicit ENABLE + FORCE: FORCE alone does not enable PostgreSQL RLS.
    for table_name in (
        "organization_addresses",
        "branch_geocode_attempts",
        "branch_geolocation_state",
        "address_change_outbox",
        "branch_address_history",
        "branch_address_audit_log",
    ):
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;")

    op.execute(
        "CREATE POLICY tenant_isolation_addr_select ON organization_addresses "
        "FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_addr_insert ON organization_addresses "
        "FOR INSERT WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_addr_update ON organization_addresses "
        "FOR UPDATE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_addr_delete ON organization_addresses "
        "FOR DELETE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute("REVOKE ALL ON organization_addresses FROM public;")
    op.execute("GRANT INSERT, UPDATE ON organization_addresses TO branch_admin;")

    op.execute(
        "CREATE POLICY geocode_attempts_tenant_isolation ON branch_geocode_attempts "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY geolocation_state_tenant_isolation ON branch_geolocation_state "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY outbox_tenant_isolation ON address_change_outbox "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_hist ON branch_address_history "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_audit_select ON branch_address_audit_log "
        "FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);"
    )

    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at := clock_timestamp();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON organization_addresses "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )

    op.execute(
        """
        CREATE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit logs are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_immutable_audit BEFORE UPDATE OR DELETE "
        "ON branch_address_audit_log FOR EACH ROW "
        "EXECUTE FUNCTION prevent_audit_mutation();"
    )

    op.execute(
        """
        CREATE FUNCTION snapshot_address_on_insert() RETURNS trigger AS $$
        BEGIN
          IF current_setting('app.skip_history_snapshot', true) = 'true' THEN
            RETURN NEW;
          END IF;

          INSERT INTO branch_address_history (
              address_id, org_id, dek_version, address_line1, address_line2,
              city, state_province, country_code, postal_code,
              formatted_address, valid_from, changed_by
          ) VALUES (
              NEW.id, NEW.org_id, NEW.dek_version, NEW.address_line1,
              NEW.address_line2, NEW.city, NEW.state_province,
              NEW.country_code, NEW.postal_code, NEW.formatted_address,
              clock_timestamp(),
              NULLIF(current_setting('app.current_user_id', true), '')::UUID
          );
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_snapshot_address_on_insert AFTER INSERT "
        "ON organization_addresses FOR EACH ROW "
        "EXECUTE FUNCTION snapshot_address_on_insert();"
    )

    op.execute(
        """
        CREATE FUNCTION snapshot_address_on_change() RETURNS trigger AS $$
        DECLARE
          v_now timestamptz := clock_timestamp();
        BEGIN
          IF NEW._reencryption_in_progress = TRUE THEN
            IF ROW(OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code)
               IS NOT DISTINCT FROM
               ROW(NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN
              NEW._reencryption_in_progress := FALSE;
              RETURN NEW;
            END IF;
            RAISE EXCEPTION
              'plaintext fields mutated during KMS re-encryption pass: address_id=%',
              OLD.id;
          END IF;

          IF ROW(
                OLD.address_line1, OLD.address_line2, OLD.city,
                OLD.state_province, OLD.country_code, OLD.postal_code
             ) IS DISTINCT FROM ROW(
                NEW.address_line1, NEW.address_line2, NEW.city,
                NEW.state_province, NEW.country_code, NEW.postal_code
             ) THEN
            UPDATE branch_address_history
            SET valid_to = v_now
            WHERE address_id = OLD.id AND valid_to IS NULL;

            INSERT INTO branch_address_history (
                address_id, org_id, dek_version, address_line1, address_line2,
                city, state_province, country_code, postal_code,
                formatted_address, valid_from, changed_by
            ) VALUES (
                OLD.id, OLD.org_id, OLD.dek_version, OLD.address_line1,
                OLD.address_line2, OLD.city, OLD.state_province,
                OLD.country_code, OLD.postal_code, OLD.formatted_address,
                v_now,
                NULLIF(current_setting('app.current_user_id', true), '')::UUID
            );

            INSERT INTO branch_address_audit_log (
                event_id, address_id, org_id, dek_version,
                old_address, new_address, changed_by,
                ip_address, user_agent, request_id
            ) VALUES (
                gen_random_uuid(), OLD.id, OLD.org_id, OLD.dek_version,
                jsonb_build_object(
                    'city', OLD.city,
                    'state', OLD.state_province,
                    'country_code', OLD.country_code,
                    'postal_code', OLD.postal_code,
                    'dek_version', OLD.dek_version,
                    'address_line1_hash', encode(sha256(OLD.address_line1::bytea), 'hex')
                ),
                jsonb_build_object(
                    'city', NEW.city,
                    'state', NEW.state_province,
                    'country_code', NEW.country_code,
                    'postal_code', NEW.postal_code,
                    'dek_version', NEW.dek_version,
                    'address_line1_hash', encode(sha256(NEW.address_line1::bytea), 'hex')
                ),
                NULLIF(current_setting('app.current_user_id', true), '')::UUID,
                NULLIF(current_setting('app.ip_address', true), '')::INET,
                NULLIF(current_setting('app.user_agent', true), ''),
                NULLIF(current_setting('app.request_id', true), '')::UUID
            );

            INSERT INTO address_change_outbox (
                address_id, org_id, event_type, payload
            ) VALUES (
                NEW.id,
                NEW.org_id,
                'address_updated',
                jsonb_build_object('address_id', NEW.id, 'timestamp', v_now)
            );
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_snapshot_address_history BEFORE UPDATE "
        "ON organization_addresses FOR EACH ROW "
        "EXECUTE FUNCTION snapshot_address_on_change();"
    )

    op.execute(
        """
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug,
          b.timezone, b.currency_code, b.region_code, b.country_code,
          b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public,
          s.version, s.updated_at AS state_updated_at
        FROM org_branches b
        JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
        """
    )
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = true);")

    op.execute(
        """
        CREATE VIEW v_public_branch_addresses WITH (security_barrier = true) AS
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
          AND (
              g.validation_status = 'success'
              OR g.last_known_good_coordinates IS NOT NULL
          )
          AND a.org_id = NULLIF(
              current_setting('app.current_org_id', true), ''
          )::UUID;
        """
    )
    op.execute("ALTER VIEW v_public_branch_addresses SET (security_invoker = true);")
    op.execute("GRANT SELECT ON v_public_branch_addresses TO branch_viewer;")


def downgrade() -> None:
    """Remove only 00f-owned capability after proving rollback is lossless."""
    _preflight_downgrade()

    op.execute("DROP VIEW v_public_branch_addresses;")
    op.execute("REVOKE SELECT ON v_public_branch_addresses FROM branch_viewer;")
    # The REVOKE above is intentionally harmless if ownership already dropped
    # with the view; privileges on a dropped view cannot survive.

    op.execute("DROP VIEW v_active_org_branches;")

    _restore_address_predecessor_security()

    for table_name, policy_name in (
        ("branch_geocode_attempts", "geocode_attempts_tenant_isolation"),
        ("branch_geolocation_state", "geolocation_state_tenant_isolation"),
        ("address_change_outbox", "outbox_tenant_isolation"),
        ("branch_address_history", "tenant_isolation_hist"),
        ("branch_address_audit_log", "tenant_isolation_audit_select"),
    ):
        op.execute(f"DROP POLICY {policy_name} ON {table_name};")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP TRIGGER trg_snapshot_address_history ON organization_addresses;")
    op.execute("DROP TRIGGER trg_snapshot_address_on_insert ON organization_addresses;")
    op.execute("DROP TRIGGER trg_set_updated_at ON organization_addresses;")
    op.execute("DROP TRIGGER trg_immutable_audit ON branch_address_audit_log;")

    op.execute("DROP FUNCTION snapshot_address_on_change() RESTRICT;")
    op.execute("DROP FUNCTION snapshot_address_on_insert() RESTRICT;")
    op.execute("DROP FUNCTION prevent_audit_mutation() RESTRICT;")
    op.execute("DROP FUNCTION set_updated_at() RESTRICT;")

    op.execute("DROP INDEX uq_one_default_translation_per_branch;")
    op.execute("DROP INDEX uq_one_physical_per_branch;")
    op.execute("DROP INDEX uq_branch_name_per_org;")
    op.execute("DROP INDEX uq_branch_slug_per_org_ci;")
    op.execute("DROP INDEX ix_addr_history_open_window;")

    op.drop_constraint(
        _BRANCH_ORG_FK,
        "organization_addresses",
        type_="foreignkey",
        schema="public",
    )
    op.drop_constraint(
        _DELETED_BY_FK,
        "organization_addresses",
        type_="foreignkey",
        schema="public",
    )

    # Remove the branch pointer before deleting branches synthesized by 00f.
    op.drop_column("organization_addresses", "deleted_by", schema="public")
    op.drop_column("organization_addresses", "_reencryption_in_progress", schema="public")
    op.drop_column("organization_addresses", "allow_search_indexing", schema="public")
    op.drop_column("organization_addresses", "dek_version", schema="public")
    op.drop_column("organization_addresses", "branch_id", schema="public")

    _drop_synthesized_branches()

    op.drop_table("branch_geolocation_state")
    op.drop_table("branch_geocode_attempts")
    op.drop_table("branch_address_history")
    op.drop_index("ix_branch_translations_search", table_name="branch_name_translations")
    op.drop_table("branch_name_translations")
    op.drop_table("branch_address_audit_log")
    op.drop_table("address_change_outbox")

    op.drop_index("ix_org_branches_normalized", table_name="org_branches")
    op.drop_index("ix_org_branches_name_trgm", table_name="org_branches")
    op.alter_column(
        "org_branches",
        "internal_slug",
        existing_type=postgresql.CITEXT(),
        type_=sa.VARCHAR(length=32),
        existing_nullable=False,
    )
    op.drop_column("org_branches", "search_normalized_name")
    op.execute("ALTER INDEX ix_org_branches_org_id_v2 RENAME TO ix_org_branches_org_id;")

    op.execute("ALTER TABLE organization_addresses DROP CONSTRAINT chk_address_type;")
    op.execute("UPDATE organization_addresses SET address_type = 'operational' WHERE address_type = 'physical';")
    op.execute("ALTER TABLE organization_addresses ALTER COLUMN address_type SET DEFAULT 'operational';")
    op.execute(
        "ALTER TABLE organization_addresses ADD CONSTRAINT ck_org_address_type "
        "CHECK (address_type IN ('registered', 'operational', 'billing'));"
    )

    op.execute(
        """
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug,
          b.timezone, b.currency_code, b.region_code, b.country_code,
          b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public,
          s.version, s.updated_at AS state_updated_at
        FROM org_branches b
        JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
        """
    )
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = true);")
