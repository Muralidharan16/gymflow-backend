import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'

with open(file_path, 'r') as f:
    content = f.read()

upgrade_sql = """
    # Setup Extensions
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        op.execute("CREATE EXTENSION IF NOT EXISTS citext;")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        
        # Concurrent Indexes
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_addr_history_open_window ON branch_address_history(address_id) WHERE valid_to IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_physical_per_branch ON organization_addresses(branch_id) WHERE address_type = 'physical' AND deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_google_place_id_per_org ON organization_addresses(org_id, google_place_id) WHERE google_place_id IS NOT NULL AND deleted_at IS NULL;")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_branch_geo_coordinates ON branch_geolocation_state USING gist(coordinates) WHERE validation_status = 'success';")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_slug_per_org_ci ON org_branches(org_id, internal_slug) WHERE deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_name_per_org ON org_branches(org_id, lower(branch_name)) WHERE deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_default_translation_per_branch ON branch_name_translations(branch_id) WHERE is_default = TRUE;")

    # RBAC Roles (Ignoring if they already exist to be safe)
    op.execute('''
    DO $$ 
    BEGIN 
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'branch_admin') THEN CREATE ROLE branch_admin; END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'branch_viewer') THEN CREATE ROLE branch_viewer; END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ops_support') THEN CREATE ROLE ops_support WITH BYPASSRLS NOLOGIN; END IF;
    END $$;
    ''')

    # RLS Policies
    op.execute('''
    ALTER TABLE organization_addresses FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation_addr_select ON organization_addresses FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    CREATE POLICY tenant_isolation_addr_insert ON organization_addresses FOR INSERT WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    CREATE POLICY tenant_isolation_addr_update ON organization_addresses FOR UPDATE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    CREATE POLICY tenant_isolation_addr_delete ON organization_addresses FOR DELETE USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    REVOKE ALL ON organization_addresses FROM public;
    GRANT INSERT, UPDATE ON organization_addresses TO branch_admin;

    ALTER TABLE branch_geocode_attempts FORCE ROW LEVEL SECURITY;
    CREATE POLICY geocode_attempts_tenant_isolation ON branch_geocode_attempts USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);

    ALTER TABLE address_change_outbox FORCE ROW LEVEL SECURITY;
    CREATE POLICY outbox_tenant_isolation ON address_change_outbox USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);

    ALTER TABLE branch_address_history FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation_hist ON branch_address_history USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);

    ALTER TABLE branch_address_audit_log FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation_audit_select ON branch_address_audit_log FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    ''')

    # Exclusion constraint
    op.execute('''
    ALTER TABLE branch_address_history ADD CONSTRAINT no_overlapping_address_versions EXCLUDE USING gist (
        address_id WITH =,
        tstzrange(valid_from, COALESCE(valid_to, 'infinity')) WITH &&
    );
    ''')

    # Triggers and Functions
    op.execute('''
    CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
    BEGIN
      NEW.updated_at := clock_timestamp();
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'audit logs are immutable'; END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_immutable_audit BEFORE UPDATE OR DELETE ON branch_address_audit_log FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();

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

    CREATE TRIGGER trg_snapshot_address_on_insert AFTER INSERT ON organization_addresses FOR EACH ROW EXECUTE FUNCTION snapshot_address_on_insert();

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
          
        INSERT INTO branch_address_audit_log(address_id, org_id, dek_version, old_address, new_address, changed_by, ip_address, user_agent, request_id)
        VALUES (
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

    CREATE TRIGGER trg_snapshot_address_history BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION snapshot_address_on_change();
    ''')

    # Security Barrier View
    op.execute('''
    CREATE VIEW v_public_branch_addresses WITH (security_barrier = true) AS
    SELECT
        a.id,
        a.city, 
        a.country_code,
        a.google_place_id,
        CASE
            WHEN a.is_exact_location_visible THEN 
                COALESCE(
                    CASE WHEN g.validation_status = 'success' THEN g.coordinates END,
                    g.last_known_good_coordinates
                )
            ELSE 
                ST_SnapToGrid(
                    COALESCE(
                        CASE WHEN g.validation_status = 'success' THEN g.coordinates::geometry END,
                        g.last_known_good_coordinates::geometry
                    ), 0.01
                )::geography
        END AS coordinates
    FROM organization_addresses a
    JOIN branch_geolocation_state g ON a.id = g.address_id
    WHERE a.deleted_at IS NULL
      AND a.allow_search_indexing = TRUE
      AND (g.validation_status = 'success' OR g.last_known_good_coordinates IS NOT NULL)
      AND a.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID;

    GRANT SELECT ON v_public_branch_addresses TO branch_viewer;
    ''')
"""

downgrade_sql = """
    op.execute("DROP VIEW IF EXISTS v_public_branch_addresses;")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_history ON organization_addresses;")
    op.execute("DROP FUNCTION IF EXISTS snapshot_address_on_change();")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_address_on_insert ON organization_addresses;")
    op.execute("DROP FUNCTION IF EXISTS snapshot_address_on_insert();")
    op.execute("DROP TRIGGER IF EXISTS trg_immutable_audit ON branch_address_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation();")
    op.execute("DROP TRIGGER IF EXISTS trg_set_updated_at ON organization_addresses;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    
    op.execute("ALTER TABLE branch_address_history DROP CONSTRAINT IF EXISTS no_overlapping_address_versions;")
"""

# Insert upgrade_sql at the end of the upgrade() function
# and downgrade_sql at the beginning of the downgrade() function

def insert_sql(content, func_name, sql, position="end"):
    pattern = rf"(def {func_name}\(\) -> None:\s+[\"'].*?[\"']\s+# ### commands auto generated by Alembic - please adjust! ###\n)"
    parts = re.split(pattern, content)
    
    if len(parts) != 3:
        return content

    if position == "end":
        # Find the end of the function.
        # It's everything up to "# ### end Alembic commands ###" or the next function
        func_body, rest = parts[2].split("# ### end Alembic commands ###", 1)
        new_body = parts[0] + parts[1] + func_body + sql + "\n    # ### end Alembic commands ###" + rest
        return new_body
    else:
        new_body = parts[0] + parts[1] + sql + "\n" + parts[2]
        return new_body

content = insert_sql(content, "upgrade", upgrade_sql, "end")
content = insert_sql(content, "downgrade", downgrade_sql, "start")

with open(file_path, 'w') as f:
    f.write(content)
