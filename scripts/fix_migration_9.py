import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

# Comment out or remove the drop_index of idx_places_cache_expires
content = content.replace("op.drop_index(op.f('idx_places_cache_expires'), table_name='google_places_cache')", "# op.drop_index(op.f('idx_places_cache_expires'), table_name='google_places_cache')")
content = content.replace("op.create_index(op.f('idx_places_cache_expires'), 'google_places_cache', ['expires_at'], unique=False)", "# op.create_index(op.f('idx_places_cache_expires'), 'google_places_cache', ['expires_at'], unique=False)")

with open(file_path, 'w') as f:
    f.write(content)

