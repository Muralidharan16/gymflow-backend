import pytest
from sqlalchemy.pool import NullPool
from app.core.database import async_engine

@pytest.fixture(scope="session", autouse=True)
def disable_async_engine_pooling():
    # Force NullPool to prevent connections from being shared across different event loops
    async_engine.pool = NullPool(async_engine.pool._creator)
