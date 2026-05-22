import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

content = content.replace("op.execute(\"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_slug_per_org_ci ON org_branches(org_id, internal_slug) WHERE deleted_at IS NULL;\")",
                          "op.execute(\"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_slug_per_org_ci ON org_branches(org_id, internal_slug);\")")

content = content.replace("op.execute(\"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_name_per_org ON org_branches(org_id, lower(branch_name)) WHERE deleted_at IS NULL;\")",
                          "op.execute(\"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_branch_name_per_org ON org_branches(org_id, lower(branch_name));\")")

with open(file_path, 'w') as f:
    f.write(content)

