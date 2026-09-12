"""init_geo_infrastructure

Revision ID: 16c65fdfd9a8
Revises: 66a95af89112
Create Date: 2026-05-25 19:34:34.219402

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16c65fdfd9a8'
down_revision: Union[str, Sequence[str], None] = '66a95af89112'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MIGRATION_OWNER = "migration_owner"
_GEO_TYPES = (
    "geo_import_status",
    "geo_record_status",
)
_GEO_TABLES = (
    "countries",
    "subdivisions",
    "cities",
    "postal_codes",
    "country_name_aliases",
    "subdivision_name_aliases",
    "city_name_aliases",
    "geo_audit_log",
    "geo_postal_overrides",
    "geo_raw_import_files",
    "geo_import_jobs",
    "geo_quarantined_records",
)
_UPDATED_AT_TRIGGER_TABLES = (
    "countries",
    "subdivisions",
    "cities",
    "postal_codes",
)


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_migration_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name,
                role_data.rolsuper,
                role_data.rolinherit,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError("16c geo migration requires session_user=migration_owner")
    if row["current_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError("16c geo migration requires current_user=migration_owner")
    if any(
        bool(row[name])
        for name in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner is over-privileged for 16c geo migration")


def _require_infrastructure_citext(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                extension_data.extversion::text AS extension_version,
                owner_role.rolname::text AS owner_name
            FROM pg_catalog.pg_extension AS extension_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = extension_data.extowner
            WHERE extension_data.extname = 'citext'
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            "16c geo migration requires infrastructure-provisioned citext"
        )
    if row["owner_name"] != "postgres":
        raise RuntimeError(
            "16c geo migration requires citext to remain infrastructure-owned by postgres"
        )


def _require_predecessor_set_updated_at(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                owner_role.rolname::text AS owner_name,
                language_data.lanname::text AS language_name,
                routine_data.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
                    AS returns_trigger,
                routine_data.prosecdef AS security_definer,
                routine_data.provolatile::text AS volatility,
                routine_data.proconfig IS NULL AS has_no_config,
                pg_catalog.pg_get_function_identity_arguments(routine_data.oid)::text
                    AS identity_arguments,
                pg_catalog.btrim(
                    pg_catalog.regexp_replace(
                        routine_data.prosrc,
                        '[[:space:]]+',
                        ' ',
                        'g'
                    )
                ) AS normalized_source
            FROM pg_catalog.pg_proc AS routine_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = routine_data.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = routine_data.proowner
            JOIN pg_catalog.pg_language AS language_data
              ON language_data.oid = routine_data.prolang
            WHERE namespace_data.nspname = 'public'
              AND routine_data.proname = 'set_updated_at'
              AND pg_catalog.pg_get_function_identity_arguments(routine_data.oid) = ''
            """
        )
    ).mappings().one_or_none()
    expected_source = (
        "BEGIN NEW.updated_at := clock_timestamp(); RETURN NEW; END;"
    )
    if row is None:
        raise RuntimeError(
            "16c geo migration requires predecessor public.set_updated_at()"
        )
    if (
        row["owner_name"] != _MIGRATION_OWNER
        or row["language_name"] != "plpgsql"
        or not row["returns_trigger"]
        or row["security_definer"]
        or row["volatility"] != "v"
        or not row["has_no_config"]
        or row["identity_arguments"] != ""
        or row["normalized_source"] != expected_source
    ):
        raise RuntimeError(
            "16c predecessor public.set_updated_at() contract drifted: "
            f"{dict(row)!r}"
        )


def _require_geo_absent(bind) -> None:
    present_tables = bind.execute(
        sa.text(
            """
            SELECT required.table_name
            FROM pg_catalog.unnest(CAST(:tables AS text[])) AS required(table_name)
            WHERE pg_catalog.to_regclass('public.' || required.table_name) IS NOT NULL
            ORDER BY required.table_name
            """
        ),
        {"tables": list(_GEO_TABLES)},
    ).scalars().all()
    if present_tables:
        raise RuntimeError(
            f"16c geo relation collision before upgrade: {tuple(present_tables)!r}"
        )

    present_types = bind.execute(
        sa.text(
            """
            SELECT required.type_name
            FROM pg_catalog.unnest(CAST(:types AS text[])) AS required(type_name)
            WHERE pg_catalog.to_regtype('public.' || required.type_name) IS NOT NULL
            ORDER BY required.type_name
            """
        ),
        {"types": list(_GEO_TYPES)},
    ).scalars().all()
    if present_types:
        raise RuntimeError(
            f"16c geo type collision before upgrade: {tuple(present_types)!r}"
        )


def _require_geo_present(bind) -> None:
    missing_tables = bind.execute(
        sa.text(
            """
            SELECT required.table_name
            FROM pg_catalog.unnest(CAST(:tables AS text[])) AS required(table_name)
            WHERE pg_catalog.to_regclass('public.' || required.table_name) IS NULL
            ORDER BY required.table_name
            """
        ),
        {"tables": list(_GEO_TABLES)},
    ).scalars().all()
    if missing_tables:
        raise RuntimeError(
            f"16c geo relations missing from owned surface: {tuple(missing_tables)!r}"
        )

    wrong_owners = bind.execute(
        sa.text(
            """
            SELECT namespace_data.nspname || '.' || relation_data.relname
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = ANY(CAST(:tables AS text[]))
              AND relation_data.relkind IN ('r', 'p')
              AND owner_role.rolname <> :owner
            ORDER BY relation_data.relname
            """
        ),
        {"tables": list(_GEO_TABLES), "owner": _MIGRATION_OWNER},
    ).scalars().all()
    if wrong_owners:
        raise RuntimeError(
            f"16c geo relation ownership drift: {tuple(wrong_owners)!r}"
        )

    type_rows = bind.execute(
        sa.text(
            """
            SELECT
                type_data.typname::text AS type_name,
                owner_role.rolname::text AS owner_name,
                type_data.typtype::text AS type_kind
            FROM pg_catalog.pg_type AS type_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = type_data.typnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = type_data.typowner
            WHERE namespace_data.nspname = 'public'
              AND type_data.typname = ANY(CAST(:types AS text[]))
            ORDER BY type_data.typname
            """
        ),
        {"types": list(_GEO_TYPES)},
    ).mappings().all()
    if len(type_rows) != len(_GEO_TYPES):
        raise RuntimeError(
            f"16c geo enum inventory drift: {tuple(dict(row) for row in type_rows)!r}"
        )
    for row in type_rows:
        if row["owner_name"] != _MIGRATION_OWNER or row["type_kind"] != "e":
            raise RuntimeError(
                f"16c geo enum contract drift: {dict(row)!r}"
            )

    trigger_rows = bind.execute(
        sa.text(
            """
            SELECT relation_data.relname::text AS table_name,
                   trigger_data.tgname::text AS trigger_name
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = ANY(CAST(:tables AS text[]))
              AND trigger_data.tgname = ('set_updated_at_' || relation_data.relname)
              AND NOT trigger_data.tgisinternal
            ORDER BY relation_data.relname
            """
        ),
        {"tables": list(_UPDATED_AT_TRIGGER_TABLES)},
    ).mappings().all()
    observed_trigger_tables = {row["table_name"] for row in trigger_rows}
    if observed_trigger_tables != set(_UPDATED_AT_TRIGGER_TABLES):
        raise RuntimeError(
            "16c set_updated_at trigger inventory drift: "
            f"observed={sorted(observed_trigger_tables)!r}"
        )


def _preflight(bind, *, direction: str) -> None:
    _require_migration_identity(bind)
    _require_infrastructure_citext(bind)
    _require_predecessor_set_updated_at(bind)
    if direction == "upgrade":
        _require_geo_absent(bind)
    elif direction == "downgrade":
        _require_geo_present(bind)
    else:
        raise RuntimeError(f"unsupported 16c migration direction {direction!r}")


def _postflight(bind, *, direction: str) -> None:
    _require_infrastructure_citext(bind)
    _require_predecessor_set_updated_at(bind)
    if direction == "upgrade":
        _require_geo_present(bind)
    elif direction == "downgrade":
        _require_geo_absent(bind)
    else:
        raise RuntimeError(f"unsupported 16c migration direction {direction!r}")


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind, direction="upgrade")

    # citext and public.set_updated_at() are predecessor/infrastructure-owned.
    # This revision consumes them; it does not install or overwrite them.

    # 1. ENUM Types
    op.execute("CREATE TYPE public.geo_import_status AS ENUM ('running', 'validating', 'promoted', 'failed');")
    op.execute("CREATE TYPE public.geo_record_status AS ENUM ('active', 'deprecated', 'historical', 'pending_validation');")

    # 2. Canonical Tables
    op.execute("""
    CREATE TABLE public.countries (
        id SMALLSERIAL PRIMARY KEY,
        iso2 CHAR(2) UNIQUE NOT NULL,
        iso3 CHAR(3) UNIQUE NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        phone_code TEXT,
        currency_code CHAR(3),
        timezone TEXT,
        status geo_record_status NOT NULL DEFAULT 'active',
        deactivated_at TIMESTAMPTZ,
        deactivation_reason TEXT,
        postal_code_regex TEXT,
        postal_code_min_length SMALLINT,
        postal_code_max_length SMALLINT,
        supports_postal_lookup BOOLEAN DEFAULT TRUE,
        ui_config JSONB,
        source_priority SMALLINT DEFAULT 100,
        confidence_score SMALLINT DEFAULT 100 CHECK (confidence_score BETWEEN 0 AND 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    op.execute("CREATE INDEX idx_countries_status ON public.countries(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_countries BEFORE UPDATE ON public.countries FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();")

    op.execute("""
    CREATE TABLE public.subdivisions (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES public.countries(id) ON DELETE RESTRICT,
        parent_id BIGINT REFERENCES public.subdivisions(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        code TEXT,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        type TEXT NOT NULL,
        timezone TEXT,
        valid_from DATE,
        valid_until DATE,
        status geo_record_status NOT NULL DEFAULT 'active',
        deactivated_at TIMESTAMPTZ,
        deactivation_reason TEXT,
        source_priority SMALLINT DEFAULT 100,
        confidence_score SMALLINT DEFAULT 100 CHECK (confidence_score BETWEEN 0 AND 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(id, country_id),
        UNIQUE(country_id, normalized_name, type),
        CONSTRAINT chk_subdiv_temporal CHECK (valid_until IS NULL OR valid_until >= valid_from),
        CONSTRAINT chk_subdiv_historical CHECK (status != 'historical' OR valid_until IS NOT NULL)
    );
    """)
    op.execute("CREATE INDEX idx_subdiv_country ON public.subdivisions(country_id);")
    op.execute("CREATE INDEX idx_subdiv_norm_name ON public.subdivisions(normalized_name);")
    op.execute("CREATE INDEX idx_subdiv_status ON public.subdivisions(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_subdivisions BEFORE UPDATE ON public.subdivisions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();")

    op.execute("""
    CREATE TABLE public.cities (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES public.countries(id) ON DELETE RESTRICT,
        subdivision_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        latitude NUMERIC(9,6) CHECK (latitude BETWEEN -90 AND 90),
        longitude NUMERIC(9,6) CHECK (longitude BETWEEN -180 AND 180),
        timezone TEXT,
        valid_from DATE,
        valid_until DATE,
        status geo_record_status NOT NULL DEFAULT 'active',
        deactivated_at TIMESTAMPTZ,
        deactivation_reason TEXT,
        source_priority SMALLINT DEFAULT 100,
        confidence_score SMALLINT DEFAULT 100 CHECK (confidence_score BETWEEN 0 AND 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(id, country_id),
        FOREIGN KEY (subdivision_id, country_id) REFERENCES public.subdivisions(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        UNIQUE(country_id, subdivision_id, normalized_name),
        CONSTRAINT chk_cities_temporal CHECK (valid_until IS NULL OR valid_until >= valid_from),
        CONSTRAINT chk_cities_historical CHECK (status != 'historical' OR valid_until IS NOT NULL)
    );
    """)
    op.execute("CREATE INDEX idx_cities_subdiv ON public.cities(subdivision_id);")
    op.execute("CREATE INDEX idx_cities_norm_name ON public.cities(normalized_name);")
    op.execute("CREATE INDEX idx_cities_status ON public.cities(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_cities BEFORE UPDATE ON public.cities FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();")

    op.execute("""
    CREATE TABLE public.postal_codes (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES public.countries(id) ON DELETE RESTRICT,
        subdivision_id BIGINT NOT NULL,
        city_id BIGINT NOT NULL,
        postal_code TEXT NOT NULL,
        locality TEXT,
        locality_normalized TEXT GENERATED ALWAYS AS (COALESCE(locality, '__NULL__')) STORED,
        latitude NUMERIC(9,6) CHECK (latitude BETWEEN -90 AND 90),
        longitude NUMERIC(9,6) CHECK (longitude BETWEEN -180 AND 180),
        timezone TEXT,
        valid_from DATE,
        valid_until DATE,
        replaced_by_postal_code_id BIGINT REFERENCES public.postal_codes(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        status geo_record_status NOT NULL DEFAULT 'active',
        deactivated_at TIMESTAMPTZ,
        deactivation_reason TEXT,
        source TEXT,
        source_version TEXT,
        import_batch_id UUID,
        imported_at TIMESTAMPTZ,
        source_priority SMALLINT DEFAULT 100,
        confidence_score SMALLINT DEFAULT 100 CHECK (confidence_score BETWEEN 0 AND 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        FOREIGN KEY (subdivision_id, country_id) REFERENCES public.subdivisions(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (city_id, country_id) REFERENCES public.cities(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT uq_country_postal_locality UNIQUE(country_id, postal_code, locality_normalized),
        CONSTRAINT chk_postal_temporal CHECK (valid_until IS NULL OR valid_until >= valid_from),
        CONSTRAINT chk_postal_historical CHECK (status != 'historical' OR valid_until IS NOT NULL)
    );
    """)
    op.execute("CREATE INDEX idx_postal_codes_lookup ON public.postal_codes(country_id, postal_code) WHERE status = 'active';")
    op.execute("CREATE INDEX idx_postal_codes_city ON public.postal_codes(city_id);")
    op.execute("CREATE TRIGGER set_updated_at_postal_codes BEFORE UPDATE ON public.postal_codes FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();")

    # 3. Alias Tables
    op.execute("""
    CREATE TABLE public.country_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES public.countries(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)
    op.execute("""
    CREATE TABLE public.subdivision_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        subdivision_id BIGINT NOT NULL REFERENCES public.subdivisions(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)
    op.execute("""
    CREATE TABLE public.city_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        city_id BIGINT NOT NULL REFERENCES public.cities(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)

    # 4. Audit System
    op.execute("""
    CREATE TABLE public.geo_audit_log (
        id BIGSERIAL PRIMARY KEY,
        table_name TEXT NOT NULL,
        record_id BIGINT NOT NULL,
        change_type TEXT NOT NULL,
        old_data JSONB,
        new_data JSONB,
        changed_at TIMESTAMPTZ DEFAULT NOW(),
        changed_by TEXT
    );
    """)
    op.execute("CREATE INDEX idx_geo_audit ON public.geo_audit_log(table_name, record_id);")

    op.execute("""
    CREATE TABLE public.geo_postal_overrides (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES public.countries(id),
        postal_code TEXT NOT NULL,
        locality TEXT,
        corrected_city_id BIGINT REFERENCES public.cities(id),
        corrected_subdivision_id BIGINT REFERENCES public.subdivisions(id),
        status geo_record_status NOT NULL DEFAULT 'active',
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by TEXT
    );
    """)
    op.execute("CREATE INDEX idx_geo_overrides_lookup ON public.geo_postal_overrides(country_id, postal_code) WHERE status = 'active';")

    # 5. Import System
    op.execute("""
    CREATE TABLE public.geo_raw_import_files (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT REFERENCES public.countries(id),
        source_name TEXT NOT NULL,
        version TEXT,
        parser_version TEXT NOT NULL,
        file_blob_path TEXT NOT NULL,
        checksum TEXT NOT NULL UNIQUE,
        source_url TEXT,
        imported_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(source_name, version)
    );
    """)

    op.execute("""
    CREATE TABLE public.geo_import_jobs (
        id UUID PRIMARY KEY,
        country_id SMALLINT REFERENCES public.countries(id),
        raw_file_id BIGINT REFERENCES public.geo_raw_import_files(id),
        idempotency_key TEXT UNIQUE NOT NULL,
        source_name TEXT NOT NULL,
        version TEXT,
        parser_version TEXT NOT NULL,
        status geo_import_status NOT NULL DEFAULT 'running',
        started_at TIMESTAMPTZ DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        imported_rows INTEGER DEFAULT 0,
        invalid_rows INTEGER DEFAULT 0,
        promoted_rows INTEGER DEFAULT 0,
        failure_reason TEXT
    );
    """)

    op.execute("""
    CREATE TABLE public.geo_quarantined_records (
        id BIGSERIAL PRIMARY KEY,
        import_batch_id UUID REFERENCES public.geo_import_jobs(id),
        raw_payload JSONB NOT NULL,
        failure_reason TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        quarantined_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

    _postflight(bind, direction="upgrade")


def downgrade() -> None:
    bind = op.get_bind()
    _preflight(bind, direction="downgrade")

    # Detach revision-owned triggers explicitly. Do not drop or replace the
    # predecessor-owned public.set_updated_at() function.
    for table_name in reversed(_UPDATED_AT_TRIGGER_TABLES):
        op.execute(
            f"DROP TRIGGER set_updated_at_{table_name} ON public.{table_name};"
        )

    # Drop revision-owned relations in dependency-safe reverse order. No
    # CASCADE/IF EXISTS is used: unknown dependencies or missing objects are
    # lifecycle drift and must fail closed.
    op.execute("DROP TABLE public.geo_quarantined_records;")
    op.execute("DROP TABLE public.geo_import_jobs;")
    op.execute("DROP TABLE public.geo_raw_import_files;")
    op.execute("DROP TABLE public.geo_postal_overrides;")
    op.execute("DROP TABLE public.geo_audit_log;")
    op.execute("DROP TABLE public.city_name_aliases;")
    op.execute("DROP TABLE public.subdivision_name_aliases;")
    op.execute("DROP TABLE public.country_name_aliases;")
    op.execute("DROP TABLE public.postal_codes;")
    op.execute("DROP TABLE public.cities;")
    op.execute("DROP TABLE public.subdivisions;")
    op.execute("DROP TABLE public.countries;")

    op.execute("DROP TYPE public.geo_import_status;")
    op.execute("DROP TYPE public.geo_record_status;")

    _postflight(bind, direction="downgrade")
