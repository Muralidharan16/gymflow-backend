"""
app/core/middleware.py
======================
Enterprise middleware stack for the Doers SaaS platform.

Stack (outermost → innermost in FastAPI add_middleware order, bottom → top):
  SecurityHeadersMiddleware   — HSTS, CSP, X-Frame-Options
  CorrelationIdMiddleware     — X-Request-ID propagation
  OpenTelemetryTraceMiddleware— W3C traceparent inject/extract, span enrichment
  RedisRateLimiterMiddleware  — Lua token-bucket (integer microtokens, Redis TIME)
  AdaptiveWriteThrottler      — EWMA-based priority-aware probabilistic shedding
  TenantMiddleware            — JWT validation, blacklist, state injection
  IdempotencyHeaderMiddleware — Fast-path replay check via X-Idempotency-Key header
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
import uuid
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.security import ACCESS_TOKEN_PRINCIPAL_TYPES, decode_token
from app.core.redis import redis_client
from app.core.concurrency import adaptive_controller

logger = logging.getLogger("doers.middleware")

# ─────────────────────────────────────────────────────────────────────────────
# Path exemptions
# ─────────────────────────────────────────────────────────────────────────────

EXEMPT_PATHS = {
    "/auth/signup",
    "/auth/signup-status",
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
    "/organizations/mock-s3/upload",
    "/health",
    "/api/v1/finance/payments/webhooks/razorpay",
}


PREFIX_EXEMPT_PATHS = {"/static"}


def _is_exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in PREFIX_EXEMPT_PATHS)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Security Headers
# ─────────────────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        from app.core.drain import drain_coordinator
        await drain_coordinator.increment_inflight()
        try:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"]  = "nosniff"
            response.headers["X-Frame-Options"]          = "SAMEORIGIN"
            response.headers["X-XSS-Protection"]         = "1; mode=block"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"]  = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "frame-src https://www.google.com https://maps.google.com; "
                "child-src https://www.google.com https://maps.google.com;"
            )
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            return response
        finally:
            await drain_coordinator.decrement_inflight()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Correlation ID
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationIdMiddleware:
    """Pure-ASGI correlation propagation without BaseHTTP task/stream overhead."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id

        async def send_with_correlation(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", correlation_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_correlation)

# ─────────────────────────────────────────────────────────────────────────────
# 3. OpenTelemetry Trace Middleware
# ─────────────────────────────────────────────────────────────────────────────

try:
    from opentelemetry import trace, context as otel_context
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _tracer     = trace.get_tracer("doers.platform.tracer")
    _propagator = TraceContextTextMapPropagator()
    _OTEL_OK    = True
except ImportError:
    _OTEL_OK = False


class OpenTelemetryTraceMiddleware(BaseHTTPMiddleware):
    """
    Extracts W3C traceparent from inbound headers, creates a server span,
    enriches it with request + response metadata, and propagates context
    into request.state for downstream use.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not _OTEL_OK:
            return await call_next(request)

        carrier  = {k.lower(): v for k, v in request.headers.items()}
        ctx      = _propagator.extract(carrier=carrier)
        start_ms = time.monotonic()

        with _tracer.start_as_current_span(
            f"{request.method} {request.url.path}",
            context=ctx,
            kind=trace.SpanKind.SERVER,
        ) as span:
            span_ctx = span.get_span_context()
            if span_ctx.is_valid:
                request.state.otel_trace_id = format(span_ctx.trace_id, "032x")
                request.state.otel_span_id  = format(span_ctx.span_id, "016x")
            else:
                request.state.otel_trace_id = request.state.__dict__.get("correlation_id", "unknown")
                request.state.otel_span_id  = "unknown"

            tenant_id = request.headers.get("X-Tenant-ID", "unknown")
            span.set_attribute("tenant.id",    tenant_id)
            span.set_attribute("http.method",  request.method)
            span.set_attribute("http.route",   request.url.path)

            try:
                response = await call_next(request)
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("http.latency_ms",  (time.monotonic() - start_ms) * 1000)
                return response
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise


# ─────────────────────────────────────────────────────────────────────────────
# 4. Redis Token-Bucket Rate Limiter (integer microtokens, Redis TIME)
# ─────────────────────────────────────────────────────────────────────────────

_LUA_TENANT_ADMISSION = """
local sem_key  = KEYS[1]
local rate_key = KEYS[2]
local tier_key = KEYS[3]

local limit = tonumber(ARGV[1])
local token = ARGV[2]
local ttl   = tonumber(ARGV[3])

-- One Redis-server clock drives both the semaphore lease and token bucket.
local now_data = redis.call('TIME')
local now_us   = tonumber(now_data[1]) * 1000000 + tonumber(now_data[2])
local now_s    = now_us / 1000000

-- Concurrency admission. Return code 0 retains the existing concurrency 429.
redis.call('ZREMRANGEBYSCORE', sem_key, '-inf', now_s - ttl)
local count = redis.call('ZCARD', sem_key)
if count >= limit then
    return 0
end
redis.call('ZADD', sem_key, now_s, token)

-- Resolve the live tenant tier inside the same atomic script, avoiding a
-- separate network round trip while keeping Redis as the source of truth.
local tier = redis.call('GET', tier_key)
local capacity_us
local fill_rate
if tier == 'ENTERPRISE' then
    capacity_us = tonumber(ARGV[6])
    fill_rate   = tonumber(ARGV[7])
else
    capacity_us = tonumber(ARGV[4])
    fill_rate   = tonumber(ARGV[5])
end

local bucket    = redis.call('HMGET', rate_key, 'tokens_us', 'last_us')
local tokens_us = tonumber(bucket[1] or capacity_us)
local last_us   = tonumber(bucket[2] or now_us)

local elapsed = now_us - last_us
local refill  = math.floor(elapsed * fill_rate)
tokens_us     = math.min(capacity_us, tokens_us + refill)

local cost_us = 1000000
if tokens_us < cost_us then
    -- Do not leak a concurrency lease when rate admission is denied.
    redis.call('ZREM', sem_key, token)
    return 2
end

redis.call('HMSET', rate_key, 'tokens_us', tokens_us - cost_us, 'last_us', now_us)
redis.call('EXPIRE', rate_key, 3600)
return 1
"""

_LUA_SEM_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""


class RedisRateLimiterMiddleware:
    """
    Pure-ASGI tenant admission middleware.

    Concurrency leases and live-tier token-bucket admission are decided by one
    atomic Redis script using Redis-server time. Avoiding BaseHTTPMiddleware
    removes task/stream orchestration from the authenticated request hot path.

    Tier defaults:
      ENTERPRISE   → 5000 tokens capacity, 500/s fill
      default      → 600 tokens capacity,  60/s fill
    """
    _LEASE_TTL = 60
    _BURST_LIMIT = 150

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        tenant_raw = dict(scope.get("headers") or ()).get(b"x-tenant-id")
        if not tenant_raw:
            await self.app(scope, receive, send)
            return
        tenant_id = tenant_raw.decode("latin-1")

        sem_key = f"concurrency_lease:{tenant_id}"
        rate_key = f"rate_limit:{tenant_id}"
        tier_key = f"tenant_tier:{tenant_id}"
        token = str(uuid.uuid4())

        try:
            decision = await redis_client.eval(
                _LUA_TENANT_ADMISSION,
                3,
                sem_key,
                rate_key,
                tier_key,
                self._BURST_LIMIT,
                token,
                self._LEASE_TTL,
                600_000_000,
                60_000,
                5_000_000_000,
                500_000,
            )
        except Exception:
            decision = None
        if decision is None:
            # Preserve the certified degraded mode: readiness reports Redis loss,
            # while otherwise-authorized application traffic remains available.
            await self.app(scope, receive, send)
            return
        if decision in (2, "2"):
            response = JSONResponse(status_code=429, content={"detail": "Rate limit exceeded."})
            await response(scope, receive, send)
            return
        if decision not in (1, "1"):
            response = JSONResponse(status_code=429, content={"detail": "Concurrency limit reached."})
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            await redis_client.eval(_LUA_SEM_RELEASE, 1, sem_key, token)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Adaptive Write Throttler (EWMA-based, priority-aware)
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveWriteThrottler:
    """
    Pure-ASGI adaptive write shedding.

    Reads pass directly to the next ASGI app. Writes preserve the existing Redis
    backpressure flag, priority calculation, rejection probability, headers and
    response body without BaseHTTPMiddleware task/stream overhead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                is_throttled = await redis_client.get("backpressure:write_throttle_active")
            except Exception:
                # Preserve the certified degraded mode when Redis is absent or
                # not initialized (for example isolated FastAPI boundary tests).
                is_throttled = None
            if is_throttled == b"true" or is_throttled == "true":
                reject_prob = adaptive_controller.rejection_probability(scope.get("path", ""))
                if reject_prob > 0 and random.random() < reject_prob:
                    response = JSONResponse(
                        status_code=429,
                        headers={
                            "X-Envoy-Overloaded": "true",
                            "Retry-After": "5",
                        },
                        content={
                            "detail": "Server under backpressure. Retry after a moment.",
                            "reason": "adaptive_backpressure",
                            "ewma_latency_ms": adaptive_controller.ewma_latency_ms,
                        },
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tenant Authentication Middleware
# ─────────────────────────────────────────────────────────────────────────────

class TenantMiddleware:
    """
    Pure-ASGI JWT tenant authentication.

    Preserves the existing token validation, Redis revocation checks, degraded
    Redis behavior, and request.state contract while avoiding BaseHTTPMiddleware
    task/stream orchestration on every authenticated request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if _is_exempt(request.url.path) or request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        auth = request.headers.get("Authorization", "")
        token = None

        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]
        else:
            token = request.cookies.get("access_token")

        if not token:
            response = JSONResponse(status_code=401, content={"detail": "Missing authentication."})
            await response(scope, receive, send)
            return

        try:
            payload = decode_token(token)
        except Exception:
            response = JSONResponse(status_code=401, content={"detail": "Invalid or expired token."})
            await response(scope, receive, send)
            return

        principal_type = payload.get("principal_type")
        if principal_type not in ACCESS_TOKEN_PRINCIPAL_TYPES:
            response = JSONResponse(status_code=401, content={"detail": "Invalid principal type."})
            await response(scope, receive, send)
            return

        jti = payload.get("jti")
        family_id = payload.get("f_id")

        try:
            if jti and await redis_client.get(f"blacklist:{jti}"):
                response = JSONResponse(status_code=401, content={"detail": "Token revoked."})
                await response(scope, receive, send)
                return
            if family_id and await redis_client.get(f"family_revoked:{family_id}"):
                response = JSONResponse(status_code=401, content={"detail": "Session revoked."})
                await response(scope, receive, send)
                return
        except Exception:
            pass  # Redis unavailable — proceed (fail-open; blacklist check is defense-in-depth)

        request.state.staff_id = payload.get("sub")
        request.state.principal_type = principal_type
        request.state.org_id = payload.get("org_id")
        request.state.gym_id = payload.get("gym_id")
        request.state.role = payload.get("role")
        request.state.branch_ids = payload.get("branch_ids", [])

        await self.app(scope, receive, send)

# ─────────────────────────────────────────────────────────────────────────────
# 7. Idempotency Header Fast-Path Middleware
# ─────────────────────────────────────────────────────────────────────────────

class IdempotencyMiddleware:
    """
    Pure-ASGI fast-path replay for simple X-Idempotency-Key requests.

    The durable database-backed idempotency engine remains authoritative. This
    middleware preserves the existing Redis cache semantics while removing
    BaseHTTPMiddleware overhead from the read-heavy request path.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if request.method not in ("POST", "PATCH", "PUT"):
            await self.app(scope, receive, send)
            return

        ikey = request.headers.get("X-Idempotency-Key")
        if not ikey:
            await self.app(scope, receive, send)
            return

        user_id = getattr(request.state, "staff_id", "anon")
        cache_key = f"idempotency:{user_id}:{ikey}"

        try:
            cached = await redis_client.get(cache_key)
            if cached and cached != "processing":
                try:
                    data = json.loads(cached)
                    response = Response(
                        content=data["body"],
                        status_code=data["status_code"],
                        headers=data.get("headers", {}),
                    )
                    await response(scope, receive, send)
                    return
                except Exception:
                    pass
        except Exception:
            pass

        status_code: int | None = None

        async def capture_status(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        await self.app(scope, receive, capture_status)

        if status_code is not None and status_code < 400:
            try:
                await redis_client.setex(cache_key, 3600, "processing")
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# JWT sub extractor (rate-limit identity — no signature verification)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_sub_from_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
    else:
        token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub")
    except Exception:
        return None