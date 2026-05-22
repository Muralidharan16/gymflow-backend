import re

file_path = '/home/jeevashri/gymflow-backend/alembic/versions/af160d6e2348_add_hyperscale_branch_name_and_address_.py'
with open(file_path, 'r') as f:
    content = f.read()

# We want to replace lines like:
#     op.drop_index(op.f('uq_member_primary_address'), table_name='member_addresses', postgresql_where='((is_primary = true) AND (deleted_at IS NULL))')
# with:
#     try:
#         op.drop_index(op.f('uq_member_primary_address'), table_name='member_addresses', postgresql_where='((is_primary = true) AND (deleted_at IS NULL))')
#     except Exception:
#         pass

# Regex to match op.drop_index(...) or op.drop_constraint(...) lines, potentially multi-line.
# We'll do it line by line if we can, but some might span multiple lines.
# Actually, we can search for the start of `op.drop_index` or `op.drop_constraint` and wrap them.

def wrap_statement(content, stmt_type):
    # Match op.stmt_type(anything) until the matching parenthesis at the end of the statement.
    # Since statements can be multi-line, we can use a parser or regex.
    # Let's use a regex that matches op.stmt_type( ... )
    # Since they are relatively simple, we can find instances of op.stmt_type
    
    pos = 0
    while True:
        pos = content.find(f"op.{stmt_type}(", pos)
        if pos == -1:
            break
        
        # Find matching closing parenthesis
        # We start counting parentheses from the character after the '('
        start_paren = pos + len(f"op.{stmt_type}(")
        count = 1
        i = start_paren
        while count > 0 and i < len(content):
            if content[i] == '(':
                count += 1
            elif content[i] == ')':
                count -= 1
            i += 1
        
        stmt = content[pos:i]
        
        # We wrap this statement in a try-except block
        # Determine indentation
        indent_match = re.match(r'^\s*', content[max(0, content.rfind('\n', 0, pos))+1 : pos])
        indent = indent_match.group(0) if indent_match else '    '
        
        wrapped = f"try:\n{indent}    {stmt}\n{indent}except Exception:\n{indent}    pass"
        
        content = content[:pos] + wrapped + content[i:]
        pos += len(wrapped) # Move past the wrapped statement to avoid infinite loop

    return content

content = wrap_statement(content, "drop_index")
content = wrap_statement(content, "drop_constraint")

with open(file_path, 'w') as f:
    f.write(content)

