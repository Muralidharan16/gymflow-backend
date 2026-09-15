"""P8 request observability middleware.

These middlewares add evidence only. Authorization remains owned by the existing
TenantMiddleware and P1-P7 request boundaries.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.observability.context import (
    UNKNOWN,
    bind_observability_context,
    normalize_identifier,
    request_authority_context,
    reset_observability_context,
)


logger = logging.getLogger("doers.observability.request")
_CORRELATION_HEADER = b"x-request-id"


def _replace_scope_header(scope: dict[str, Any], name: bytes, value: str) -> None:
    headers = [
        (key, existing_value)
        for key, existing_value in scope.get("headers", [])
        if key.lower() != name
    ]
    headers.append((name, value.encode("ascii")))
    scope["headers"] = headers


def _safe_correlation_id(request: Request, request_id: str) -> str:
    incoming = request.headers.get("X-Request-ID")
    normalized = normalize_identifier(incoming, max_length=128)
    return request_id if normalized == UNKNOWN else normalized


def _bind_final_request_context(request: Request):
    return bind_observability_context(**request_authority_context(request))


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """Own server request IDs, sanitize correlation IDs, and log completion."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        correlation_id = _safe_correlation_id(request, request_id)
        _replace_scope_header(request.scope, _CORRELATION_HEADER, correlation_id)
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        outer_tokens = bind_observability_context(
            request_id=request_id,
            correlation_id=correlation_id,
        )
        started = time.monotonic()
        logger.info(
            "API request started",
            extra={
                "event": "api.request.started",
                "http_method": request.method,
                "http_path": request.url.path,
            },
        )
        try:
            response = await call_next(request)
            final_tokens = _bind_final_request_context(request)
            try:
                logger.info(
                    "API request completed",
                    extra={
                        "event": "api.request.completed",
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "http_status_code": response.status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
            finally:
                reset_observability_context(final_tokens)
            response.headers["X-Doers-Request-ID"] = request_id
            return response
        except Exception:
            final_tokens = _bind_final_request_context(request)
            try:
                logger.exception(
                    "API request failed",
                    extra={
                        "event": "api.request.failed",
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
            finally:
                reset_observability_context(final_tokens)
            raise
        finally:
            reset_observability_context(outer_tokens)


class AuthenticatedObservabilityContextMiddleware(BaseHTTPMiddleware):
    """Bind authoritative tenant/principal/trace context around endpoint execution."""

    async def dispatch(self, request: Request, call_next) -> Response:
        tokens = bind_observability_context(**request_authority_context(request))
        try:
            return await call_next(request)
        finally:
            reset_observability_context(tokens)
