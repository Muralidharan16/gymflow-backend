import asyncio
import os
import sys

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pyrefly: ignore [missing-import]
from sqlalchemy import text
from app.core.database import async_engine

async def reset_alembic():
    async with async_engine.begin() as conn:
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('001_initial_schema')"))
        print("Reset alembic_version to 001_initial_schema")

if __name__ == "__main__":
    asyncio.run(reset_alembic())
