import asyncio
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

load_dotenv()

def require_destructive_reset_enabled():
    if os.getenv("ALLOW_DESTRUCTIVE_DB_RESET") != "true":
        raise SystemExit("Refusing destructive DB reset. Set ALLOW_DESTRUCTIVE_DB_RESET=true to continue.")

async def drop():
    engine = create_async_engine(os.getenv('DATABASE_URL'))
    async with engine.begin() as conn:
        await conn.execute(text('DROP SCHEMA public CASCADE'))
        await conn.execute(text('CREATE SCHEMA public'))
    print("Dropped public schema")

require_destructive_reset_enabled()
asyncio.run(drop())
