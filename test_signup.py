import asyncio
from httpx import AsyncClient
from app.main import app
import re

async def test():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        # Signup
        response = await ac.post(
            "/auth/signup",
            json={
                "email": "integration_test@example.com",
                "password": "Password123!",
                "owner_name": "Test User",
                "facility_type": "gym",
                "org_name": "Test Org"
            }
        )
        print("Signup:", response.status_code, response.json())
        
        # Read from redis to get the token (simulate email)
        from app.core.redis import get_redis_utils
        import hashlib
        ru = get_redis_utils()
        await ru._ensure_scripts_loaded()
        email_hash = hashlib.sha256(b"integration_test@example.com").hexdigest()
        token_hash = await ru.client.get(f"signup:email:{email_hash}")
        print("Token Hash:", token_hash)
        
        # we don't have the raw token, but verify expects raw token...
        # wait! verify() takes the raw token, hashes it, and looks it up.
        # But we don't have the raw token because it's sent via email.
        # So we can't test verify via API unless we mock secrets.token_urlsafe.
