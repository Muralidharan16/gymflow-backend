import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

content = content.replace('op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")', '')

with open(file_path, 'w') as f:
    f.write(content)
