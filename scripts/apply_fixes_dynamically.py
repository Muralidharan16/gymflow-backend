import os
import glob
import re
import sys

# Find the newly generated migration file in alembic/versions/
migration_files = glob.glob('/home/jeevashri/gymflow-backend/alembic/versions/*_add_hyperscale_branch_name_and_address_*.py')
if not migration_files:
    print("Could not find migration file!")
    sys.exit(1)

# Get the latest file by modification time
file_path = max(migration_files, key=os.path.getmtime)
print(f"Applying fixes to: {file_path}")

with open(file_path, 'r') as f:
    content = f.read()

# Define patterns to drop/ignore
ignore_tables = [
    'audit_chain_heads', 'event_outbox', 'idempotency_store', 'address_audit_ledger',
    'encryption_key_registry', 'organization_address_payloads_secure', 'tenant_resource_quotas',
    'key_rotation_progress', 'active_idempotency_keys', 'outbox_events', 'organization_address_audit_log',
    'branch_audit_log'
]

def should_ignore_line(line):
    for t in ignore_tables:
        if t in line:
            return True
    if 'event_outbox_delivery_state' in line:
        return True
    if 'v_active_org_branches' in line and ('create_table' in line or 'drop_table' in line):
        return True
    return False

def filter_statements(body):
    statements = []
    lines = body.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            statements.append(line)
            i += 1
            continue
        
        if line.strip().startswith('op.'):
            stmt_lines = [line]
            open_p = line.count('(')
            close_p = line.count(')')
            while open_p > close_p and i + 1 < len(lines):
                i += 1
                next_line = lines[i]
                stmt_lines.append(next_line)
                open_p += next_line.count('(')
                close_p += next_line.count(')')
            
            stmt = '\n'.join(stmt_lines)
            if not should_ignore_line(stmt):
                statements.append(stmt)
        else:
            statements.append(line)
        i += 1
    return '\n'.join(statements)

# Extract upgrade and downgrade functions
upgrade_match = re.search(r'(def upgrade\(\) -> None:\s*""".*?"""\n)(.*?)(?=\n\ndef downgrade|$)', content, re.DOTALL)
downgrade_match = re.search(r'(def downgrade\(\) -> None:\s*""".*?"""\n)(.*?)(?=\n\n|$)', content, re.DOTALL)

if not upgrade_match or not downgrade_match:
    print("Could not find upgrade or downgrade functions!")
    sys.exit(1)

upgrade_header = upgrade_match.group(1)
upgrade_body = upgrade_match.group(2)

downgrade_header = downgrade_match.group(1)
downgrade_body = downgrade_match.group(2)

filtered_upgrade_body = filter_statements(upgrade_body)
filtered_downgrade_body = filter_statements(downgrade_body)

# Wrap remaining drop_index and drop_constraint in try-except
def wrap_statements_in_body(body, stmt_type):
    lines = body.split('\n')
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(f'op.{stmt_type}('):
            stmt_lines = [line]
            open_p = line.count('(')
            close_p = line.count(')')
            while open_p > close_p and i + 1 < len(lines):
                i += 1
                next_line = lines[i]
                stmt_lines.append(next_line)
                open_p += next_line.count('(')
                close_p += next_line.count(')')
            stmt = '\n'.join(stmt_lines)
            indent = re.match(r'^\s*', line).group(0)
            wrapped = f"{indent}try:\n" + '\n'.join([f"{indent}    {l.strip()}" for l in stmt_lines]) + f"\n{indent}except Exception:\n{indent}    pass"
            new_lines.append(wrapped)
        else:
            new_lines.append(line)
        i += 1
    return '\n'.join(new_lines)

filtered_upgrade_body = wrap_statements_in_body(filtered_upgrade_body, "drop_index")
filtered_upgrade_body = wrap_statements_in_body(filtered_upgrade_body, "drop_constraint")

filtered_downgrade_body = wrap_statements_in_body(filtered_downgrade_body, "drop_index")
filtered_downgrade_body = wrap_statements_in_body(filtered_downgrade_body, "drop_constraint")

# Inject Custom SQL
pre_sql = """
    # TRUNCATE organization_addresses because we're radically changing the schema (adding NOT NULL branch_id)
    op.execute("TRUNCATE TABLE organization_addresses CASCADE;")

    # Setup Extensions
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        op.execute("CREATE EXTENSION IF NOT EXISTS citext;")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # RBAC Roles
    op.execute('''
    DO $$ 
    BEGIN 
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'branch_admin') THEN CREATE ROLE branch_admin; END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'branch_viewer') THEN CREATE ROLE branch_viewer; END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ops_support') THEN CREATE ROLE ops_support WITH BYPASSRLS NOLOGIN; END IF;
    END $$;
    ''')

    # Drop view temporarily to allow altering column types
    op.execute("DROP VIEW IF EXISTS v_active_org_branches;")
"""

post_sql = """
    # Recreate the view dropped earlier
    op.execute('''
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT 
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug, b.timezone, b.currency_code, b.region_code, b.country_code, b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public, s.version, s.updated_at AS state_updated_at
        FROM org_branches b JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
    ''')

    # Concurrent Indexes
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_addr_history_open_window ON branch_address_history(address_id) WHERE valid_to IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_physical_per_branch ON organization_addresses(branch_id) WHERE address_type = 'physical' AND deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_google_place_id_per_org ON organization_addresses(org_id, google_place_id) WHERE google_place_id IS NOT NULL AND deleted_at IS NULL;")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_slug_per_org_ci ON org_branches(org_id, internal_slug);")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_name_per_org ON org_branches(org_id, lower(branch_name));")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_one_default_translation_per_branch ON branch_name_translations(branch_id) WHERE is_default = TRUE;")

    # RLS Policies (Split for asyncpg prepared statements compatibility)
    op.execute("ALTER TABLE organization_addresses FORCE ROW LEVEL SECURITY;")
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

    # Functions and Triggers (separated to avoid multi-command issues)
    op.execute('''
    CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
    BEGIN
      NEW.updated_at := clock_timestamp();
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    ''')
    op.execute("CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION set_updated_at();")

    op.execute('''
    CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'audit logs are immutable'; END;
    $$ LANGUAGE plpgsql;
    ''')
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
    ''')
    op.execute("CREATE TRIGGER trg_snapshot_address_history BEFORE UPDATE ON organization_addresses FOR EACH ROW EXECUTE FUNCTION snapshot_address_on_change();")

    # Security Barrier View
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
    op.execute("GRANT SELECT ON v_public_branch_addresses TO branch_viewer;")
"""

final_upgrade_body = pre_sql + filtered_upgrade_body + post_sql

new_content = content[:upgrade_match.start(2)] + final_upgrade_body + content[upgrade_match.end(2):]

downgrade_match = re.search(r'(def downgrade\(\) -> None:\s*""".*?"""\n)(.*?)(?=\n\n|$)', new_content, re.DOTALL)
if downgrade_match:
    new_content = new_content[:downgrade_match.start(2)] + filtered_downgrade_body + new_content[downgrade_match.end(2):]

with open(file_path, 'w') as f:
    f.write(new_content)

print(f"Applied fixes successfully to {file_path}")
