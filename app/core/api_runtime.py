"""P7 API process runtime controls.

This module deliberately contains no tenant authorization.  The system probe paths
are safe read-only process signals, while preStop uses its own orchestration secret.
Request draining is enforced before ordinary application middleware can perform
business work.
"""

from __future__ import annotations

import secrets
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.drain import PodDrainCoordinator, drain_coordinator


PRESTOP_HEADER_NAME: Final[str] = "X-Doers-PreStop-Token"
PRESTOP_ENV_NAME: Final[str] = "DOERS_PRESTOP_CONTROL_TOKEN"
MIN_PRODUCTION_PRESTOP_TOKEN_BYTES: Final[int] = 32

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
    """Validate a dedicated preStop credential without exposing secret material.

    Missing server configuration always fails closed.  Production additionally
    requires at least 32 bytes so an accidentally weak deployment secret cannot
    silently become pod-drain authority.
    """

    if expected_token is None or not expected_token or expected_token.isspace():
        raise PreStopConfigurationError("preStop control token is not configured")
    if production and len(expected_token.encode("utf-8")) < MIN_PRODUCTION_PRESTOP_TOKEN_BYTES:
        raise PreStopConfigurationError("preStop control token is too short")

    candidate = presented_token or ""
    return secrets.compare_digest(candidate, expected_token)


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


class P7SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers without owning P7 in-flight accounting."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        return _apply_security_headers(response)


class DrainAdmissionMiddleware(BaseHTTPMiddleware):
    """Atomically stop admitting ordinary requests once drain begins.

    System probes/control requests and CORS preflight are not business in-flight
    work.  All other requests are admitted and counted under one coordinator lock,
    closing the race between observing readiness and incrementing the counter.
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
        if request.url.path in SYSTEM_REQUEST_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        if not await self._coordinator.try_admit_request():
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
