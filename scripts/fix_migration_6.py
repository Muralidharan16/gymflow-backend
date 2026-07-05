import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

drop_view_sql = """
    # Drop view temporarily to allow altering column types
    op.execute("DROP VIEW IF EXISTS v_active_org_branches;")
"""

recreate_view_sql = """
    # Recreate the view dropped earlier
    op.execute('''
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT 
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug, b.timezone, b.currency_code, b.region_code, b.country_code, b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public, s.version, s.updated_at AS state_updated_at
        FROM org_branches b JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
    ''')
"""

# Insert drop_view_sql before the first op.alter_column
alter_col_pattern = "    op.alter_column('allowed_branch_transitions', 'from_status',"

content = content.replace(alter_col_pattern, drop_view_sql + "\n" + alter_col_pattern)

# Insert recreate_view_sql in post_sql, before `CREATE OR REPLACE VIEW v_public_branch_addresses`
v_public_pattern = "    # Security Barrier View\n    op.execute('''\n    CREATE OR REPLACE VIEW v_public_branch_addresses WITH (security_barrier = true) AS"

content = content.replace(v_public_pattern, recreate_view_sql + "\n" + v_public_pattern)

with open(file_path, 'w') as f:
    f.write(content)

