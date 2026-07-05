import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

# Extract the block of sql that I injected previously
sql_start = "    # Setup Extensions"
sql_end = "    GRANT SELECT ON v_public_branch_addresses TO branch_viewer;\n    ''')"

start_idx = content.find(sql_start)
end_idx = content.find(sql_end) + len(sql_end)

if start_idx != -1 and end_idx != -1:
    injected_sql = content[start_idx:end_idx]
    # Remove from end
    content = content[:start_idx] + content[end_idx:]
    
    # Inject at the start of upgrade() right after docstring
    pattern = r"(def upgrade\(\) -> None:\n\s+[\"'].*?[\"']\n)"
    parts = re.split(pattern, content, maxsplit=1)
    
    if len(parts) == 3:
        # parts[0] is everything before upgrade()
        # parts[1] is the def upgrade()... docstring...
        # parts[2] is everything after
        new_content = parts[0] + parts[1] + injected_sql + "\n" + parts[2]
        with open(file_path, 'w') as f:
            f.write(new_content)

