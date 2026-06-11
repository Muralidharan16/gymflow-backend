import asyncio
import os
import sys

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pyrefly: ignore [missing-import]
from sqlalchemy import text
from app.core.database import async_engine

def require_destructive_reset_enabled():
    if os.getenv("ALLOW_DESTRUCTIVE_DB_RESET") != "true":
        raise SystemExit("Refusing destructive DB reset. Set ALLOW_DESTRUCTIVE_DB_RESET=true to continue.")

async def clean_db():
    async with async_engine.begin() as conn:
        # Drop all tables in public schema
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO postgres"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        print("Dropped and recreated public schema")

if __name__ == "__main__":
    require_destructive_reset_enabled()
    asyncio.run(clean_db())
