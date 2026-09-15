"""Authoritative P8 trace enrichment.

The legacy trace middleware starts the server span before tenant authentication.
This middleware runs inside that span but outside rate limiting/authentication
short-circuits; on every exit it overwrites caller-controlled placeholders with
trusted request state (or explicit ``unknown``) before the span ends.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.observability.context import request_authority_context
from app.observability.runtime_metrics import safe_route_template

try:
    from opentelemetry import trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency is locked in production
    _OTEL_AVAILABLE = False


def _resolved_route_template(request: Request) -> str:
    route = request.scope.get("route")
    return safe_route_template(getattr(route, "path", None))


def _apply_authoritative_span_attributes(request: Request) -> None:
    if not _OTEL_AVAILABLE:
        return
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return

    context = request_authority_context(request)
    route = _resolved_route_template(request)
    method = str(request.method or "UNKNOWN").upper()

    # These values either came from verified request.state or are explicit
    # unknowns. Never read tenant/branch/principal identity from HTTP headers.
    span.set_attribute("tenant.id", context["tenant_id"])
    span.set_attribute("branch.id", context["branch_id"])
    span.set_attribute("principal.id", context["principal_id"])
    span.set_attribute("principal.type", context["principal_type"])
    span.set_attribute("request.id", context["request_id"])
    span.set_attribute("correlation.id", context["correlation_id"])
    span.set_attribute("saga.id", context["saga_id"])
    span.set_attribute("task.id", context["task_id"])
    span.set_attribute("http.route", route)
    span.update_name(f"{method} {route}")


class AuthoritativeTraceContextMiddleware(BaseHTTPMiddleware):
    """Finalize P8 span identity/route attributes from trusted application state."""

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            return await call_next(request)
        finally:
            # Runs for normal responses, rate-limit/adaptive rejections and
            # exceptions, so untrusted legacy placeholder attributes never reach
            # an ended/exported span.
            _apply_authoritative_span_attributes(request)
