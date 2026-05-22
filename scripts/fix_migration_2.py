import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

# Tables that were wrongly dropped or created because they are unmapped or views
ignore_tables = [
    'v_active_org_branches'
]

lines = content.split('\n')
new_lines = []
skip = False
for line in lines:
    if any(f"table_name='{t}" in line or f"'{t}" in line for t in ignore_tables) and 'op.drop_' in line:
        continue
    if any(f"'{t}'" in line for t in ignore_tables) and 'op.create_table' in line:
        skip = True
    
    if skip:
        if line.strip() == ')':
            skip = False
        continue

    # Also drop index for unmapped tables
    if 'op.drop_index' in line and any(f"table_name='{t}" in line for t in ignore_tables):
        continue
    if 'op.create_index' in line and any(f", '{t}" in line for t in ignore_tables):
        continue

    new_lines.append(line)

with open(file_path, 'w') as f:
    f.write('\n'.join(new_lines))
