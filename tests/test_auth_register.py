import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from typing import AsyncGenerator
import sys
import os
import uuid
import hashlib
from unittest.mock import patch

# Ensure app is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.models.auth import Owner
from app.models.auth_session import AuthSession, AuthSessionFamily
from app.models.gym import Gym
from app.models.organization import Organization
from app.core.database import AsyncSessionLocal, get_db
from app.core.redis import init_redis, get_redis_utils

@pytest_asyncio.fixture(autouse=True)
async def cleanup_database_and_redis():
    """Flush Redis and clean up Postgres test data before and after each test."""
    from app.core.redis import init_redis, get_redis_utils, close_redis
    await close_redis()
    await init_redis()
    redis_utils = get_redis_utils()
    await redis_utils.client.flushdb()

    async with AsyncSessionLocal() as session:
        test_emails = [
            "arjun@example.com",
            "duplicate@example.com",
            "weak@example.com",
            "rate@example.com",
            "fail@example.com",
            "login@example.com",
            "locked@example.com"
        ]
        
        # Get org_ids of owners we are about to delete
        stmt = select(Owner.org_id).where(Owner.email.in_(test_emails))
        res = await session.execute(stmt)
        org_ids = res.scalars().all()
        
        # Delete auth sessions and families
        await session.execute(delete(AuthSession).where(AuthSession.user_id.in_(
            select(Owner.id).where(Owner.email.in_(test_emails))
        )))
        
        await session.execute(delete(AuthSessionFamily).where(AuthSessionFamily.user_id.in_(
            select(Owner.id).where(Owner.email.in_(test_emails))
        )))
        
        # Delete owners
        await session.execute(delete(Owner).where(Owner.email.in_(test_emails)))
        
        # Delete gyms
        if org_ids:
            await session.execute(delete(Gym).where(Gym.org_id.in_(org_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
            
        # Delete any dangling test organizations by name
        stmt_orgs = select(Organization.id).where(Organization.name.in_(["Fit Core", "Gym A", "Gym B", "Gym C", "Gym Rate Limit", "Fail Gym"]))
        res_orgs = await session.execute(stmt_orgs)
        dangling_org_ids = res_orgs.scalars().all()
        if dangling_org_ids:
            await session.execute(delete(Gym).where(Gym.org_id.in_(dangling_org_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(dangling_org_ids)))

        await session.commit()

    yield
    await close_redis()

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_signup_happy_path(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        response = await client.post("/auth/signup", json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert "Verification email sent" in response.json()["message"]
        
        # Ensure email was sent and token was generated
        assert mock_send.called
        raw_token = mock_send.call_args[1]["raw_token"]
        assert raw_token is not None

        # Verify Redis has the pending signup entry
        redis_utils = get_redis_utils()
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        pending_data = await redis_utils.get_json(f"signup:pending:{token_hash}")
        assert pending_data is not None
        assert pending_data["email"] == "arjun@example.com"
        assert pending_data["org_name"] == "Fit Core"

@pytest.mark.asyncio
async def test_signup_weak_password(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "short",
        "facility_type": "gym"
    }
    response = await client.post("/auth/signup", json=payload)
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_signup_missing_field(client):
    payload = {
        "org_name": "Fit Core",
        "email": "arjun@example.com",
        "password": "StrongPassword123!"
    }
    response = await client.post("/auth/signup", json=payload)
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_signup_rate_limit(client):
    # Loop to hit the IP rate limit of 5 requests per 10 minutes
    # Note: Use different emails so we don't hit the anti-enumeration early success return
    for i in range(5):
        payload = {
            "org_name": f"Gym Rate Limit {i}",
            "owner_name": "Rate Limiter",
            "email": f"rate{i}@example.com",
            "password": "StrongPassword123!",
            "facility_type": "gym"
        }
        with patch("app.services.auth_service.send_verification_email", return_value=True):
            resp = await client.post("/auth/signup", json=payload)
            assert resp.status_code == 200

    # The 6th request should hit the rate limit
    payload = {
        "org_name": "Gym Rate Limit 5",
        "owner_name": "Rate Limiter",
        "email": "rate5@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    resp_rate_limited = await client.post("/auth/signup", json=payload)
    assert resp_rate_limited.status_code == 429
    assert "Too many signup attempts" in resp_rate_limited.json()["detail"]

@pytest.mark.asyncio
async def test_verify_happy_path(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        
        # Verify the signup using the token
        verify_resp = await client.get(f"/auth/verify?token={raw_token}", follow_redirects=False)
        assert verify_resp.status_code == 307
        assert "verify-success" in verify_resp.headers["location"]
        
        # Verify Postgres DB contains the newly created organization and owner
        async with AsyncSessionLocal() as session:
            stmt_owner = select(Owner).where(Owner.email == "arjun@example.com")
            res_owner = await session.execute(stmt_owner)
            owner = res_owner.scalar_one_or_none()
            assert owner is not None
            assert owner.owner_name == "Arjun Singh"
            assert owner.email_verified is True
            
            stmt_org = select(Organization).where(Organization.id == owner.org_id)
            res_org = await session.execute(stmt_org)
            org = res_org.scalar_one_or_none()
            assert org is not None
            assert org.name == "Fit Core"
            
            stmt_gym = select(Gym).where(Gym.org_id == org.id)
            res_gym = await session.execute(stmt_gym)
            gym = res_gym.scalar_one_or_none()
            assert gym is not None
            assert gym.name == "Fit Core"

        redis_utils = get_redis_utils()
        assert await redis_utils.client.get(f"signup:pending:{token_hash}") is None
        email_hash = hashlib.sha256("arjun@example.com".encode("utf-8")).hexdigest()
        assert await redis_utils.client.get(f"signup:email:{email_hash}") is None

@pytest.mark.asyncio
async def test_verify_db_failure_preserves_pending_signup_and_allows_retry(client):
    payload = {
        "org_name": "Fail Gym",
        "owner_name": "Arjun Singh",
        "email": "fail@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    redis_utils = get_redis_utils()
    assert await redis_utils.client.get(f"signup:pending:{token_hash}") is not None

    with patch("app.services.auth_service.Gym", side_effect=RuntimeError("simulated db creation failure")):
        failed_verify = await client.get(f"/auth/verify?token={raw_token}", follow_redirects=False)

    assert failed_verify.status_code == 307
    assert "verify-failed?reason=server_error" in failed_verify.headers["location"]
    assert await redis_utils.client.get(f"signup:pending:{token_hash}") is not None

    async with AsyncSessionLocal() as session:
        owner_res = await session.execute(select(Owner).where(Owner.email == "fail@example.com"))
        assert owner_res.scalar_one_or_none() is None
        org_res = await session.execute(select(Organization).where(Organization.name == "Fail Gym"))
        assert org_res.scalar_one_or_none() is None

    retry_verify = await client.get(f"/auth/verify?token={raw_token}", follow_redirects=False)
    assert retry_verify.status_code == 307
    assert "verify-success" in retry_verify.headers["location"]
    assert await redis_utils.client.get(f"signup:pending:{token_hash}") is None

@pytest.mark.asyncio
async def test_signup_duplicate_email_is_safe(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "duplicate@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}")

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send_duplicate:
        duplicate_resp = await client.post("/auth/signup", json=payload)

    assert duplicate_resp.status_code == 200
    assert duplicate_resp.json()["status"] == "success"
    assert not mock_send_duplicate.called

@pytest.mark.asyncio
async def test_resend_verification_email_failure_preserves_old_token(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        old_raw_token = mock_send.call_args[1]["raw_token"]

    redis_utils = get_redis_utils()
    old_token_hash = hashlib.sha256(old_raw_token.encode("utf-8")).hexdigest()
    old_pending_key = f"signup:pending:{old_token_hash}"
    email_hash = hashlib.sha256("arjun@example.com".encode("utf-8")).hexdigest()
    email_lookup_key = f"signup:email:{email_hash}"
    assert await redis_utils.client.get(old_pending_key) is not None

    new_raw_token = "new-resend-token"
    new_token_hash = hashlib.sha256(new_raw_token.encode("utf-8")).hexdigest()
    new_pending_key = f"signup:pending:{new_token_hash}"

    with patch("app.services.auth_service.secrets.token_urlsafe", return_value=new_raw_token), \
         patch("app.services.auth_service.send_verification_email", return_value=False):
        resend_resp = await client.post("/auth/resend-verification", json={"email": "arjun@example.com"})

    assert resend_resp.status_code == 500
    assert await redis_utils.client.get(old_pending_key) is not None
    assert await redis_utils.client.get(new_pending_key) is None
    assert await redis_utils.client.get(email_lookup_key) == old_token_hash

@pytest.mark.asyncio
async def test_verify_invalid_token(client):
    verify_resp = await client.get("/auth/verify?token=invalidtoken", follow_redirects=False)
    assert verify_resp.status_code == 307
    assert "verify-failed" in verify_resp.headers["location"]

@pytest.mark.asyncio
async def test_login_happy_path(client):
    # 1. Signup and Verify
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}")
        
    # 2. Login
    login_payload = {
        "email": "arjun@example.com",
        "password": "StrongPassword123!"
    }
    login_resp = await client.post("/auth/login", json=login_payload)
    assert login_resp.status_code == 200
    
    data = login_resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["user"]["email"] == "arjun@example.com"
    
    # Check that cookies were set
    assert "access_token" in login_resp.cookies
    assert "refresh_token" in login_resp.cookies

@pytest.mark.asyncio
async def test_login_normalizes_email_case_and_spaces(client):
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }

    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}")

    login_resp = await client.post("/auth/login", json={
        "email": "  ARJUN@EXAMPLE.COM  ",
        "password": "StrongPassword123!"
    })

    assert login_resp.status_code == 200
    data = login_resp.json()
    assert data["user"]["email"] == "arjun@example.com"

@pytest.mark.asyncio
async def test_login_invalid_credentials(client):
    # 1. Signup and Verify
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}")

    # 2. Login with wrong password
    login_payload = {
        "email": "arjun@example.com",
        "password": "WrongPassword!"
    }
    login_resp = await client.post("/auth/login", json=login_payload)
    assert login_resp.status_code == 401
    assert "attempts remaining" in login_resp.json()["detail"]

@pytest.mark.asyncio
async def test_login_account_locked(client):
    # 1. Signup and Verify
    payload = {
        "org_name": "Fit Core",
        "owner_name": "Arjun Singh",
        "email": "arjun@example.com",
        "password": "StrongPassword123!",
        "facility_type": "gym"
    }
    
    with patch("app.services.auth_service.send_verification_email", return_value=True) as mock_send:
        await client.post("/auth/signup", json=payload)
        raw_token = mock_send.call_args[1]["raw_token"]
        await client.get(f"/auth/verify?token={raw_token}")

    # 2. Perform 5 failed login attempts
    login_payload = {
        "email": "arjun@example.com",
        "password": "WrongPassword!"
    }
    for _ in range(4):
        resp = await client.post("/auth/login", json=login_payload)
        assert resp.status_code == 401

    # The 5th failed attempt should trigger the account lock
    resp_lock = await client.post("/auth/login", json=login_payload)
    assert resp_lock.status_code == 429
    assert "Account locked" in resp_lock.json()["detail"]
