import asyncio
import os
import sys

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pyrefly: ignore [missing-import]
from sqlalchemy import text
from app.core.database import async_engine

async def clean_db():
    async with async_engine.begin() as conn:
        # Drop all tables in public schema
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO postgres"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        print("Dropped and recreated public schema")

if __name__ == "__main__":
    asyncio.run(clean_db())
