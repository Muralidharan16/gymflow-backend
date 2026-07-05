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


def upgrade() -> None:
    # 1. Extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS citext;")
    
    # 2. ENUM Types
    op.execute("CREATE TYPE geo_import_status AS ENUM ('running', 'validating', 'promoted', 'failed');")
    op.execute("CREATE TYPE geo_record_status AS ENUM ('active', 'deprecated', 'historical', 'pending_validation');")

    # 3. Triggers
    op.execute("""
    CREATE OR REPLACE FUNCTION set_updated_at()
    RETURNS TRIGGER AS $$
    BEGIN
       NEW.updated_at = NOW();
       RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # 4. Canonical Tables
    op.execute("""
    CREATE TABLE countries (
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
    op.execute("CREATE INDEX idx_countries_status ON countries(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_countries BEFORE UPDATE ON countries FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    op.execute("""
    CREATE TABLE subdivisions (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
        parent_id BIGINT REFERENCES subdivisions(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
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
    op.execute("CREATE INDEX idx_subdiv_country ON subdivisions(country_id);")
    op.execute("CREATE INDEX idx_subdiv_norm_name ON subdivisions(normalized_name);")
    op.execute("CREATE INDEX idx_subdiv_status ON subdivisions(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_subdivisions BEFORE UPDATE ON subdivisions FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    op.execute("""
    CREATE TABLE cities (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
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
        FOREIGN KEY (subdivision_id, country_id) REFERENCES subdivisions(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        
        UNIQUE(country_id, subdivision_id, normalized_name),
        CONSTRAINT chk_cities_temporal CHECK (valid_until IS NULL OR valid_until >= valid_from),
        CONSTRAINT chk_cities_historical CHECK (status != 'historical' OR valid_until IS NOT NULL)
    );
    """)
    op.execute("CREATE INDEX idx_cities_subdiv ON cities(subdivision_id);")
    op.execute("CREATE INDEX idx_cities_norm_name ON cities(normalized_name);")
    op.execute("CREATE INDEX idx_cities_status ON cities(id) WHERE status = 'active';")
    op.execute("CREATE TRIGGER set_updated_at_cities BEFORE UPDATE ON cities FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    op.execute("""
    CREATE TABLE postal_codes (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
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
        replaced_by_postal_code_id BIGINT REFERENCES postal_codes(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        
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
        
        FOREIGN KEY (subdivision_id, country_id) REFERENCES subdivisions(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (city_id, country_id) REFERENCES cities(id, country_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        
        CONSTRAINT uq_country_postal_locality UNIQUE(country_id, postal_code, locality_normalized),
        CONSTRAINT chk_postal_temporal CHECK (valid_until IS NULL OR valid_until >= valid_from),
        CONSTRAINT chk_postal_historical CHECK (status != 'historical' OR valid_until IS NOT NULL)
    );
    """)
    op.execute("CREATE INDEX idx_postal_codes_lookup ON postal_codes(country_id, postal_code) WHERE status = 'active';")
    op.execute("CREATE INDEX idx_postal_codes_city ON postal_codes(city_id);")
    op.execute("CREATE TRIGGER set_updated_at_postal_codes BEFORE UPDATE ON postal_codes FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    # 5. Alias Tables
    op.execute("""
    CREATE TABLE country_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)
    op.execute("""
    CREATE TABLE subdivision_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        subdivision_id BIGINT NOT NULL REFERENCES subdivisions(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)
    op.execute("""
    CREATE TABLE city_name_aliases (
        id BIGSERIAL PRIMARY KEY,
        city_id BIGINT NOT NULL REFERENCES cities(id) ON DELETE RESTRICT,
        lang TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name CITEXT NOT NULL CHECK (normalized_name <> ''),
        is_preferred BOOLEAN DEFAULT FALSE
    );
    """)

    # 6. Audit System
    op.execute("""
    CREATE TABLE geo_audit_log (
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
    op.execute("CREATE INDEX idx_geo_audit ON geo_audit_log(table_name, record_id);")
    
    op.execute("""
    CREATE TABLE geo_postal_overrides (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT NOT NULL REFERENCES countries(id),
        postal_code TEXT NOT NULL,
        locality TEXT,
        corrected_city_id BIGINT REFERENCES cities(id),
        corrected_subdivision_id BIGINT REFERENCES subdivisions(id),
        status geo_record_status NOT NULL DEFAULT 'active',
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by TEXT
    );
    """)
    op.execute("CREATE INDEX idx_geo_overrides_lookup ON geo_postal_overrides(country_id, postal_code) WHERE status = 'active';")

    # 7. Import System
    op.execute("""
    CREATE TABLE geo_raw_import_files (
        id BIGSERIAL PRIMARY KEY,
        country_id SMALLINT REFERENCES countries(id),
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
    CREATE TABLE geo_import_jobs (
        id UUID PRIMARY KEY,
        country_id SMALLINT REFERENCES countries(id),
        raw_file_id BIGINT REFERENCES geo_raw_import_files(id),
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
    CREATE TABLE geo_quarantined_records (
        id BIGSERIAL PRIMARY KEY,
        import_batch_id UUID REFERENCES geo_import_jobs(id),
        raw_payload JSONB NOT NULL,
        failure_reason TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        quarantined_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

def downgrade() -> None:
    pass
