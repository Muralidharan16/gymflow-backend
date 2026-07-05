import re

file_path = '/home/jeevashri/gymflow-backend/scripts/apply_migration_fixes.py'
with open(file_path, 'r') as f:
    content = f.read()

# Replace the old migration filename with the new one
content = re.sub(r'versions/[a-f0-9]+_add_', 'versions/a749a365e9a5_add_', content)

with open(file_path, 'w') as f:
    f.write(content)

