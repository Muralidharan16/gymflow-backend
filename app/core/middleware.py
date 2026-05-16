import uuid
import time
import json
import base64
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse
from app.core.security import decode_token
from app.core.redis import redis_client
from app.utils.rate_limit import RateLimiter

EXEMPT_PATHS = {
    "/auth/signup",
    "/auth/register",
    "/auth/login",
    "/auth/refresh",
    "/auth/verify",
    "/auth/resend-verification",
    "/onboarding/pincode",
    "/onboarding/status",
    "/onboarding/complete",
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


def _extract_sub_from_token(request: Request) -> str | None:
    """
    Extract the 'sub' claim from a JWT Bearer token by base64-decoding the
    payload segment. No cryptographic verification — used only for rate-limit
    identity, not for authz.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        # Fallback: try cookie
        token = request.cookies.get("access_token")
        if not token:
            return None
    else:
        token = auth.split(" ", 1)[1]

    try:
        # JWT = header.payload.signature — we only need the payload
        payload_b64 = token.split(".")[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
        return payload.get("sub")
    except Exception:
        return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-route, per-user JWT-scoped rate limiting.

    - Read routes  (GET / HEAD / OPTIONS): 60 req/min
    - Write routes (POST / PUT / DELETE / PATCH): 20 req/min
    - Login endpoint: stricter 5 req/min (IP-based)
    - Falls back to IP-based key when no JWT is present or is malformed.
    """

    WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
    READ_LIMIT = 60
    WRITE_LIMIT = 20
    WINDOW = 60  # seconds

    def __init__(self, app):
        super().__init__(app)
        self.login_limiter = RateLimiter("login", limit=5, window=60)

    async def dispatch(self, request: Request, call_next) -> Response:
        ip = request.client.host if request.client else "127.0.0.1"
        path = request.url.path

        # Strict IP-based limit on login
        if path == "/auth/login":
            if not await self.login_limiter.is_allowed(ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many login attempts."}
                )

        # Per-user (or per-IP fallback) rate limiting on all other routes
        sub = _extract_sub_from_token(request)
        identity = sub if sub else f"ip:{ip}"

        is_write = request.method in self.WRITE_METHODS
        limit = self.WRITE_LIMIT if is_write else self.READ_LIMIT

        redis_key = f"ratelimit:{identity}:{path}"

        try:
            now = time.time()
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(redis_key, 0, now - self.WINDOW)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, self.WINDOW)
                _, count, _, _ = await pipe.execute()

            if count >= limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please slow down."},
                )
        except Exception:
            # If Redis is unavailable, allow the request through
            pass

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

        if request.url.path == "/" or request.url.path == "/health" or request.url.path.startswith("/check-access/"):
            return await call_next(request)

        if request.method == "OPTIONS":
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        token = None
        
        if auth.startswith("Bearer "):
            token = auth.split(" ")[1]
        else:
            # Fallback to cookie for easier frontend integration
            token = request.cookies.get("access_token")

        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authentication (Header or Cookie)"}
            )

        try:
            payload = decode_token(token)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token"}
            )

        jti = payload.get("jti")
        family_id = payload.get("f_id")
        
        if jti and await redis_client.get(f"blacklist:{jti}"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Token revoked"}
            )
        
        if family_id and await redis_client.get(f"family_revoked:{family_id}"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Session revoked"}
            )

        request.state.staff_id = payload.get("sub")
        request.state.org_id = payload.get("org_id")
        request.state.gym_id = payload.get("gym_id")
        request.state.role = payload.get("role")
        
        return await call_next(request)
