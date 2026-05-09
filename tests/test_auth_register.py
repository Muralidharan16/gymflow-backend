import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator
import sys
import os

# Ensure app is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.database import Base, get_db
from app.models.models import Gym, GymOwner, EmailVerificationToken
from passlib.context import CryptContext

# Use an in-memory SQLite database for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestingSessionLocal = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with TestingSessionLocal() as session:
        yield session

app.dependency_overrides[get_db] = override_get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

import uuid
from app.redis_client import _fallback_rate_limits

@pytest_asyncio.fixture(autouse=True)
async def prepare_database():
    """Create all tables before each test and drop them after."""
    _fallback_rate_limits.clear()
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_register_happy_path(client):
    payload = {
        "gym_name": "  Fit Core  ",
        "phone": "9876543210",
        "city": "Mumbai",
        "plan": "growth",
        "owner_name": "  Arjun   Singh ",
        "email": " ARJUN@example.com ",
        "password": "StrongPassword123!"
    }
    
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 201
    
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert "gym_id" in data
    assert "owner_id" in data
    
    # Verify DB rows
    async with TestingSessionLocal() as session:
        gym = await session.get(Gym, uuid.UUID(data["gym_id"]))
        assert gym is not None
        assert gym.name == "Fit Core"
        assert gym.city == "Mumbai"
        
        owner = await session.get(GymOwner, uuid.UUID(data["owner_id"]))
        assert owner is not None
        assert owner.name == "Arjun Singh"
        assert owner.email == "arjun@example.com"
        assert pwd_context.verify("StrongPassword123!", owner.password_hash)
        
        # Verify Token
        from sqlalchemy import select
        token_stmt = select(EmailVerificationToken).where(EmailVerificationToken.owner_id == owner.id)
        token_res = await session.execute(token_stmt)
        token = token_res.scalars().first()
        assert token is not None

@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    payload = {
        "gym_name": "Gym A",
        "phone": "123456789",
        "city": "Delhi",
        "plan": "starter",
        "owner_name": "John",
        "email": "duplicate@example.com",
        "password": "Password123"
    }
    
    # First registration
    resp1 = await client.post("/auth/register", json=payload)
    assert resp1.status_code == 201
    
    # Second registration with same email
    resp2 = await client.post("/auth/register", json=payload)
    assert resp2.status_code == 409
    assert "An account with this email already exists" in resp2.json()["detail"]

@pytest.mark.asyncio
async def test_register_weak_password(client):
    payload = {
        "gym_name": "Gym B",
        "phone": "123456789",
        "city": "Delhi",
        "plan": "starter",
        "owner_name": "John",
        "email": "weak@example.com",
        "password": "short"
    }
    
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_register_missing_field(client):
    payload = {
        "gym_name": "Gym C",
        "email": "missing@example.com",
        "password": "Password123"
    }
    
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_register_rate_limit(client):
    payload = {
        "gym_name": "Gym Rate Limit",
        "phone": "123456789",
        "city": "Delhi",
        "plan": "starter",
        "owner_name": "Rate Limiter",
        "email": "rate@example.com",
        "password": "Password123"
    }
    
    # 5 allowed requests (first 4 will 201 or 409, we just care they don't 429)
    for _ in range(5):
        resp = await client.post("/auth/register", json=payload)
        assert resp.status_code in [201, 409]
        
    # 6th request should hit rate limit
    resp_rate_limited = await client.post("/auth/register", json=payload)
    assert resp_rate_limited.status_code == 429
    assert "Too many registration attempts" in resp_rate_limited.json()["detail"]

@pytest.mark.asyncio
async def test_register_db_failure_no_leak(client, monkeypatch):
    payload = {
        "gym_name": "Fail Gym",
        "phone": "123456789",
        "city": "Delhi",
        "plan": "starter",
        "owner_name": "Fail Owner",
        "email": "fail@example.com",
        "password": "Password123"
    }
    
    # Mock db.execute to raise Exception
    async def mock_execute(*args, **kwargs):
        raise Exception("Raw database error that should not leak")
    
    # We patch AsyncSession.execute
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.execute", mock_execute)
    
    response = await client.post("/auth/register", json=payload)
    assert response.status_code == 500
    assert response.json()["detail"] == "Registration failed due to a server error. Please try again."
