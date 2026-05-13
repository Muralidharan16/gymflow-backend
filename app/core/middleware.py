import uuid
import time
import json
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from app.core.security import decode_token
from app.core.redis import redis_client
from app.utils.rate_limit import RateLimiter

EXEMPT_PATHS = {
    "/auth/signup",
    "/auth/register",
    "/auth/login",
    "/auth/refresh",
    "/docs",
    "/openapi.json",
    "/redoc",
}

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Allow Swagger UI scripts and styles
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com;"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = correlation_id
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.login_limiter = RateLimiter("login", limit=5, window=60)
        self.auth_limiter = RateLimiter("auth", limit=20, window=60)

    async def dispatch(self, request: Request, call_next) -> Response:
        ip = request.client.host if request.client else "127.0.0.1"
        if request.url.path == "/auth/login":
            if not await self.login_limiter.is_allowed(ip):
                raise HTTPException(status_code=429, detail="Too many login attempts.")
        return await call_next(request)

class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in ["POST", "PATCH", "PUT"]:
            return await call_next(request)

        idempotency_key = request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        # Unique key per user/tenant
        user_id = getattr(request.state, "staff_id", "anon")
        cache_key = f"idempotency:{user_id}:{idempotency_key}"

        cached = await redis_client.get(cache_key)
        if cached:
            data = json.loads(cached)
            return Response(
                content=data["body"],
                status_code=data["status_code"],
                headers=data["headers"]
            )

        response = await call_next(request)

        if response.status_code < 400:
            # Note: Consuming body in middleware is complex with BaseHTTPMiddleware
            # In a real production app, we would use a custom Route class or handle this at the service layer.
            # For this pass, we will mark the key as 'processing' to at least prevent concurrent double-submits.
            await redis_client.setex(cache_key, 3600, "processing")
            
        return response

class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if any(request.url.path.startswith(p) for p in EXEMPT_PATHS):
            return await call_next(request)

        if request.url.path == "/" or request.url.path.startswith("/check-access/"):
            return await call_next(request)

        if request.method == "OPTIONS":
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Authorization header")

        token = auth.split(" ")[1]
        try:
            payload = decode_token(token)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

        jti = payload.get("jti")
        family_id = payload.get("f_id")
        
        if jti and await redis_client.get(f"blacklist:{jti}"):
            raise HTTPException(status_code=401, detail="Token revoked")
        
        if family_id and await redis_client.get(f"family_revoked:{family_id}"):
            raise HTTPException(status_code=401, detail="Session revoked")

        request.state.staff_id = payload["sub"]
        request.state.org_id = payload["org_id"]
        request.state.gym_id = payload.get("gym_id")
        request.state.role = payload["role"]
        
        return await call_next(request)
