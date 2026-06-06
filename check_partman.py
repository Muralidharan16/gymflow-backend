import asyncio
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://postgres:Murali%4007@127.0.0.1:5432/gymflow')
with engine.connect() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS partman;"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;"))
    res = conn.execute(text("SELECT pg_get_function_arguments(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'partman' AND p.proname = 'create_parent';"))
    for row in res:
        print(row[0])
