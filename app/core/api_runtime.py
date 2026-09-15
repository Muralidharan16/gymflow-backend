"""P7 API process runtime controls.

System probes/control are handled by an exact-path outer middleware so tenant auth,
rate limiting, adaptive throttling, or idempotency cannot accidentally determine
pod lifecycle. Ordinary traffic is then admitted atomically by the drain coordinator.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.drain import PodDrainCoordinator, drain_coordinator
from app.observability.runtime_metrics import runtime_metrics


PRESTOP_HEADER_NAME: Final[str] = "X-Doers-PreStop-Token"
PRESTOP_ENV_NAME: Final[str] = "DOERS_PRESTOP_CONTROL_TOKEN"
MIN_PRODUCTION_PRESTOP_TOKEN_BYTES: Final[int] = 32
READINESS_DEPENDENCY_TIMEOUT_SECONDS: Final[float] = 2.0

SYSTEM_REQUEST_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/_system/live",
        "/_system/ready",
        "/_system/preStop",
        "/health",  # compatibility readiness alias
    }
)


class PreStopConfigurationError(RuntimeError):
    """Raised when the server-side preStop control secret is unusable."""


def authorize_prestop_token(
    presented_token: str | None,
    *,
    expected_token: str | None,
    production: bool,
) -> bool:
    """Validate a dedicated preStop credential without exposing secret material."""
    if expected_token is None or not expected_token or expected_token.isspace():
        raise PreStopConfigurationError("preStop control token is not configured")
    if production and len(expected_token.encode("utf-8")) < MIN_PRODUCTION_PRESTOP_TOKEN_BYTES:
        raise PreStopConfigurationError("preStop control token is too short")

    return secrets.compare_digest(presented_token or "", expected_token)


def _apply_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "frame-src https://www.google.com https://maps.google.com; "
        "child-src https://www.google.com https://maps.google.com;"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def _json_response(status_code: int, content: dict, *, headers: dict[str, str] | None = None) -> Response:
    return _apply_security_headers(
        JSONResponse(status_code=status_code, content=content, headers=headers)
    )


def _readiness_metric(ready: bool) -> None:
    runtime_metrics().api_ready(ready)


async def _readiness_response(coordinator: PodDrainCoordinator) -> Response:
    """Return bounded dependency-sensitive readiness without affecting liveness."""
    if not coordinator.is_ready:
        _readiness_metric(False)
        return _json_response(503, {"status": "not_ready", "reason": "draining"})

    async def _database_ok() -> bool:
        from sqlalchemy import text as sa_text
        from app.core.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                await session.execute(sa_text("SELECT 1"))
            return True
        except Exception:
            return False

    async def _redis_ok() -> bool:
        from app.core.redis import redis_client

        try:
            return bool(await redis_client.ping())
        except Exception:
            return False

    db_result, redis_result = await asyncio.gather(
        asyncio.wait_for(_database_ok(), timeout=READINESS_DEPENDENCY_TIMEOUT_SECONDS),
        asyncio.wait_for(_redis_ok(), timeout=READINESS_DEPENDENCY_TIMEOUT_SECONDS),
        return_exceptions=True,
    )
    db_ok = db_result is True
    redis_ok = redis_result is True

    # Drain may have started while dependency checks were in flight.
    if not coordinator.is_ready:
        _readiness_metric(False)
        return _json_response(503, {"status": "not_ready", "reason": "draining"})
    if not (db_ok and redis_ok):
        _readiness_metric(False)
        return _json_response(
            503,
            {"status": "not_ready", "reason": "dependencies_unavailable"},
        )
    _readiness_metric(True)
    return _json_response(200, {"status": "ready"})


class SystemControlMiddleware(BaseHTTPMiddleware):
    """Outermost exact-path process-control plane.

    These paths never enter tenant/rate-limit/backpressure middleware. Liveness is
    process-only, readiness is bounded and dependency-sensitive, and preStop uses a
    dedicated orchestration secret rather than JWT/tenant identity.
    """

    def __init__(
        self,
        app,
        *,
        coordinator: PodDrainCoordinator | None = None,
    ) -> None:
        super().__init__(app)
        self._coordinator = coordinator or drain_coordinator

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path not in SYSTEM_REQUEST_PATHS:
            return await call_next(request)

        if path == "/_system/live":
            if request.method != "GET":
                return _json_response(405, {"detail": "Method not allowed."})
            return _json_response(200, {"status": "alive"})

        if path in {"/_system/ready", "/health"}:
            if request.method != "GET":
                return _json_response(405, {"detail": "Method not allowed."})
            return await _readiness_response(self._coordinator)

        # preStop is a control operation. POST prevents accidental activation by
        # crawlers/probe-style GETs even before the dedicated secret is checked.
        if request.method != "POST":
            return _json_response(405, {"detail": "Method not allowed."})

        from app.core.config import settings

        try:
            authorized = authorize_prestop_token(
                request.headers.get(PRESTOP_HEADER_NAME),
                expected_token=os.environ.get(PRESTOP_ENV_NAME),
                production=settings.is_production,
            )
        except PreStopConfigurationError:
            return _json_response(
                503,
                {"detail": "preStop control is unavailable."},
            )

        if not authorized:
            return _json_response(403, {"detail": "Forbidden."})

        await self._coordinator.trigger_drain()
        _readiness_metric(False)
        return _json_response(200, {"status": "drained"})


class P7SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers without owning P7 in-flight accounting."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        return _apply_security_headers(response)


class DrainAdmissionMiddleware(BaseHTTPMiddleware):
    """Atomically stop admitting ordinary requests once drain begins."""

    def __init__(
        self,
        app,
        *,
        coordinator: PodDrainCoordinator | None = None,
    ) -> None:
        super().__init__(app)
        self._coordinator = coordinator or drain_coordinator

    async def dispatch(self, request: Request, call_next) -> Response:
        # SystemControlMiddleware handles these paths before this middleware in
        # the production stack. Keep this bypass as defense-in-depth for tests or
        # alternate ASGI composition.
        if request.url.path in SYSTEM_REQUEST_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        if not await self._coordinator.try_admit_request():
            runtime_metrics().api_drain_rejected(method=request.method)
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "5"},
                content={
                    "detail": "Pod is draining; retry on another instance.",
                    "reason": "pod_draining",
                },
            )

        try:
            return await call_next(request)
        finally:
            await self._coordinator.release_request()