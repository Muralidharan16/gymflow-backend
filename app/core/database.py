from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.core.config import settings

# ---------- Async engine (FastAPI) ----------
async_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.ENVIRONMENT == "development",
)

AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db() -> AsyncSession:  # type: ignore[misc]
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

# ---------- Sync engine (Celery tasks) ----------
# Celery tasks often run in a synchronous context or need a sync session for certain libraries.
SYNC_DATABASE_URL = settings.DATABASE_URL.replace("+asyncpg", "") # Remove +asyncpg to use default psycopg2 if available
if "postgresql" in SYNC_DATABASE_URL and "+asyncpg" not in SYNC_DATABASE_URL:
    # ensure it's just postgresql://...
    pass

sync_engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
