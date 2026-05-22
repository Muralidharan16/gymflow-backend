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

from app.core.security import decode_token
from app.core.redis import redis_client
from app.core.concurrency import adaptive_controller

logger = logging.getLogger("doers.middleware")

# ─────────────────────────────────────────────────────────────────────────────
# Path exemptions
# ─────────────────────────────────────────────────────────────────────────────

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
    "/organizations/mock-s3/upload",
    "/static",
    "/health",
}


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or any(path.startswith(p) for p in EXEMPT_PATHS)


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

class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = correlation_id
        return response


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

_LUA_RATE_LIMITER = """
local key         = KEYS[1]
local capacity_us = tonumber(ARGV[1])
local fill_rate   = tonumber(ARGV[2])

local now_data  = redis.call('TIME')
local now_us    = tonumber(now_data[1]) * 1000000 + tonumber(now_data[2])

local bucket    = redis.call('HMGET', key, 'tokens_us', 'last_us')
local tokens_us = tonumber(bucket[1] or capacity_us)
local last_us   = tonumber(bucket[2] or now_us)

local elapsed   = now_us - last_us
local refill    = math.floor(elapsed * fill_rate)
tokens_us       = math.min(capacity_us, tokens_us + refill)

local cost_us   = 1000000
if tokens_us < cost_us then
    return 0
end
redis.call('HMSET', key, 'tokens_us', tokens_us - cost_us, 'last_us', now_us)
redis.call('EXPIRE', key, 3600)
return 1
"""

_LUA_SEMAPHORE = """
local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local now   = tonumber(ARGV[2])
local token = ARGV[3]
local ttl   = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
local count = redis.call('ZCARD', key)
if count >= limit then return 0 end
redis.call('ZADD', key, now, token)
return 1
"""

_LUA_SEM_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""


class RedisRateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter using atomic Lua scripts with Redis-server TIME.
    Concurrency semaphore uses sorted-set leases with Redis-authoritative timestamps.

    Tier defaults:
      ENTERPRISE   → 5000 tokens capacity, 500/s fill
      default      → 600 tokens capacity,  60/s fill
    """
    _LEASE_TTL    = 60
    _BURST_LIMIT  = 150

    async def dispatch(self, request: Request, call_next) -> Response:
        tenant_id = request.headers.get("X-Tenant-ID")
        if not tenant_id:
            return await call_next(request)

        # --- Concurrency semaphore (sorted-set lease) ---
        sem_key = f"concurrency_lease:{tenant_id}"
        token   = str(uuid.uuid4())
        now_s   = time.time()

        try:
            acquired = await redis_client.eval(
                _LUA_SEMAPHORE, 1, sem_key,
                self._BURST_LIMIT, now_s, token, self._LEASE_TTL
            )
            if not acquired:
                return JSONResponse(status_code=429, content={"detail": "Concurrency limit reached."})
        except Exception:
            acquired = True   # Redis unavailable — allow through

        try:
            # --- Token-bucket rate limit ---
            tier     = await self._get_tier(tenant_id)
            cap, fill = (5_000_000_000, 500_000) if tier == b"ENTERPRISE" else (600_000_000, 60_000)

            try:
                allowed = await redis_client.eval(
                    _LUA_RATE_LIMITER, 1,
                    f"rate_limit:{tenant_id}",
                    cap, fill,
                )
                if not allowed:
                    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded."})
            except Exception:
                pass  # Redis unavailable — allow through

            return await call_next(request)
        finally:
            try:
                await redis_client.eval(_LUA_SEM_RELEASE, 1, sem_key, token)
            except Exception:
                pass

    async def _get_tier(self, tenant_id: str):
        try:
            return await redis_client.get(f"tenant_tier:{tenant_id}")
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Adaptive Write Throttler (EWMA-based, priority-aware)
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveWriteThrottler(BaseHTTPMiddleware):
    """
    Under backpressure (Redis flag set by Prometheus alerting or WAL monitor),
    probabilistically rejects write requests based on EWMA latency and traffic class.

    Priority tiers:
      CRITICAL  (auth, billing, sessions) → never rejected
      ELEVATED  (members, etc.)           → 25% of BULK rejection rate
      BULK      (all others)              → up to 80% rejection under extreme load
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                is_throttled = await redis_client.get("backpressure:write_throttle_active")
            except Exception:
                is_throttled = None

            if is_throttled == b"true" or is_throttled == "true":
                reject_prob = adaptive_controller.rejection_probability(request.url.path)
                if reject_prob > 0 and random.random() < reject_prob:
                    return JSONResponse(
                        status_code=429,
                        headers={
                            "X-Envoy-Overloaded": "true",
                            "Retry-After": "5",
                        },
                        content={
                            "detail":           "Server under backpressure. Retry after a moment.",
                            "reason":           "adaptive_backpressure",
                            "ewma_latency_ms":  adaptive_controller.ewma_latency_ms,
                        },
                    )
        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tenant Authentication Middleware
# ─────────────────────────────────────────────────────────────────────────────

class TenantMiddleware(BaseHTTPMiddleware):
    """
    Validates JWT, checks blacklist/family-revocation in Redis, and injects
    principal claims into request.state for downstream use.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if _is_exempt(request.url.path) or request.method == "OPTIONS":
            return await call_next(request)

        auth  = request.headers.get("Authorization", "")
        token = None

        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]
        else:
            token = request.cookies.get("access_token")

        if not token:
            return JSONResponse(status_code=401, content={"detail": "Missing authentication."})

        try:
            payload = decode_token(token)
        except Exception:
            return JSONResponse(status_code=401, content={"detail": "Invalid or expired token."})

        jti       = payload.get("jti")
        family_id = payload.get("f_id")

        try:
            if jti and await redis_client.get(f"blacklist:{jti}"):
                return JSONResponse(status_code=401, content={"detail": "Token revoked."})
            if family_id and await redis_client.get(f"family_revoked:{family_id}"):
                return JSONResponse(status_code=401, content={"detail": "Session revoked."})
        except Exception:
            pass  # Redis unavailable — proceed (fail-open; blacklist check is defense-in-depth)

        request.state.staff_id = payload.get("sub")
        request.state.org_id   = payload.get("org_id")
        request.state.gym_id   = payload.get("gym_id")
        request.state.role     = payload.get("role")

        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Idempotency Header Fast-Path Middleware
# ─────────────────────────────────────────────────────────────────────────────

class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Fast-path Redis replay for simple idempotency via X-Idempotency-Key header.
    For full DB-backed idempotency with zombie recovery, use IdempotencyEngine directly.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in ("POST", "PATCH", "PUT"):
            return await call_next(request)

        ikey = request.headers.get("X-Idempotency-Key")
        if not ikey:
            return await call_next(request)

        user_id   = getattr(request.state, "staff_id", "anon")
        cache_key = f"idempotency:{user_id}:{ikey}"

        try:
            cached = await redis_client.get(cache_key)
            if cached and cached != "processing":
                try:
                    data = json.loads(cached)
                    return Response(
                        content=data["body"],
                        status_code=data["status_code"],
                        headers=data.get("headers", {}),
                    )
                except Exception:
                    pass
        except Exception:
            pass

        response = await call_next(request)

        if response.status_code < 400:
            try:
                await redis_client.setex(cache_key, 3600, "processing")
            except Exception:
                pass

        return response


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
