import asyncio
import asyncpg
from app.core.config import settings

async def fix_db():
    conn = await asyncpg.connect(settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))
    try:
        await conn.execute('ALTER TABLE organizations DROP COLUMN IF EXISTS year_established')
        print("Column dropped successfully")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(fix_db())
