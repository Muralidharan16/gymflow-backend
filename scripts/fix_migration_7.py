import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

truncate_sql = """
    # TRUNCATE organization_addresses because we're radically changing the schema (adding NOT NULL branch_id)
    op.execute("TRUNCATE TABLE organization_addresses CASCADE;")
"""

# Insert truncate_sql after `def upgrade() -> None:`
upgrade_pattern = r"(def upgrade\(\) -> None:\n\s+[\"'].*?[\"']\n)"
content = re.sub(upgrade_pattern, r"\1" + truncate_sql, content)

with open(file_path, 'w') as f:
    f.write(content)

