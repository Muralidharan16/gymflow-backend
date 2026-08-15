"""Authenticated internal control plane for deployment lifecycle operations."""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


PRESTOP_PATH = "/_system/preStop"
INTERNAL_CONTROL_HEADER = "X-DOERS-Internal-Token"


def internal_control_token_matches(provided: str | None, expected: str | None) -> bool:
    """Compare an internal control token without allowing empty-token bypasses."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def internal_control_client_is_loopback(client_host: str | None) -> bool:
    """Allow lifecycle control only from the local network namespace."""
    if not client_host:
        return False
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


class InternalControlPlaneMiddleware(BaseHTTPMiddleware):
    """Handle lifecycle control calls before tenant/user middleware executes."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path != PRESTOP_PATH:
            return await call_next(request)

        if request.method != "POST":
            return JSONResponse(
                status_code=405,
                headers={"Allow": "POST", "Cache-Control": "no-store"},
                content={"detail": "Method not allowed"},
            )

        from app.core.config import settings

        provided = request.headers.get(INTERNAL_CONTROL_HEADER)
        client_host = request.client.host if request.client else None
        if not (
            internal_control_client_is_loopback(client_host)
            and internal_control_token_matches(provided, settings.INTERNAL_CONTROL_TOKEN)
        ):
            return JSONResponse(
                status_code=403,
                headers={"Cache-Control": "no-store"},
                content={"detail": "Forbidden"},
            )

        from app.core.drain import drain_coordinator

        await drain_coordinator.trigger_drain()
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={"status": "drained"},
        )
