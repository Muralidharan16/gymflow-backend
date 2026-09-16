from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.middleware import CorrelationIdMiddleware
from app.core.telemetry import sentry_before_send
from app.observability import celery_context
from app.observability.context import (
    UNKNOWN,
    bind_observability_context,
    current_observability_context,
    request_authority_context,
    reset_observability_context,
)
from app.observability.redaction import REDACTED, redact_mapping, redact_text
from app.observability.request_context import (
    AuthenticatedObservabilityContextMiddleware,
    RequestObservabilityMiddleware,
)
from app.observability.structured_logging import StructuredJsonFormatter


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app/main.py").read_text(encoding="utf-8")
CELERY_SOURCE = (ROOT / "app/core/celery_app.py").read_text(encoding="utf-8")


def test_recursive_redaction_is_non_mutating_and_covers_sensitive_classes() -> None:
    original = {
        "authorization": "Bearer super-secret-token",
        "nested": {
            "password": "hunter2",
            "email": "member@example.com",
            "phone_number": "+91 98765 43210",
            "safe": "operational-value",
        },
        "database_url": "postgresql://user:password@db/internal",
        "address_line1": "12 Anna Salai",
    }
    safe = redact_mapping(original)

    assert safe["authorization"] == REDACTED
    assert safe["nested"]["password"] == REDACTED
    assert safe["nested"]["email"] == REDACTED
    assert safe["nested"]["phone_number"] == REDACTED
    assert safe["nested"]["safe"] == "operational-value"
    assert safe["database_url"] == REDACTED
    assert safe["address_line1"] == REDACTED
    assert original["nested"]["password"] == "hunter2"
    assert original["authorization"] == "Bearer super-secret-token"


def test_free_text_redaction_covers_secret_pii_and_preserves_iso_date() -> None:
    raw = (
        "password=hunter2 Authorization: Bearer abc.def.ghi "
        "member@example.com +91 98765 43210 10.10.20.30 "
        "postgresql://dbuser:dbpass@postgres/internal on 2026-09-15"
    )
    safe = redact_text(raw)
    for leaked in (
        "hunter2",
        "abc.def.ghi",
        "member@example.com",
        "98765 43210",
        "10.10.20.30",
        "dbuser",
        "dbpass",
    ):
        assert leaked not in safe
    assert "2026-09-15" in safe
    assert REDACTED in safe


def test_structured_formatter_emits_context_and_redacts_message_extra_and_exception() -> None:
    tokens = bind_observability_context(
        request_id="request-1",
        correlation_id="corr-1",
        tenant_id="tenant-1",
        branch_id="branch-1",
        principal_id="principal-1",
        principal_type="staff",
        saga_id="saga-1",
        task_id="task-1",
        trace_id="trace-1",
        span_id="span-1",
    )
    try:
        record = logging.LogRecord(
            name="doers.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed for member@example.com password=hunter2",
            args=(),
            exc_info=None,
        )
        record.event = "test.failure"
        record.payload = {
            "api_key": "key-123",
            "safe": "kept",
            "nested": {"phone": "+91 98765 43210"},
        }
        payload = json.loads(StructuredJsonFormatter().format(record))
    finally:
        reset_observability_context(tokens)

    assert payload["request_id"] == "request-1"
    assert payload["correlation_id"] == "corr-1"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["branch_id"] == "branch-1"
    assert payload["principal_id"] == "principal-1"
    assert payload["saga_id"] == "saga-1"
    assert payload["task_id"] == "task-1"
    assert payload["trace_id"] == "trace-1"
    assert payload["span_id"] == "span-1"
    assert "member@example.com" not in payload["message"]
    assert "hunter2" not in payload["message"]
    assert payload["fields"]["event"] == "test.failure"
    assert payload["fields"]["payload"]["api_key"] == REDACTED
    assert payload["fields"]["payload"]["safe"] == "kept"
    assert payload["fields"]["payload"]["nested"]["phone"] == REDACTED


def test_sentry_uses_shared_recursive_redaction() -> None:
    event = {
        "request": {
            "headers": {"Authorization": "Bearer sentry-secret", "Cookie": "sid=secret"},
            "data": {"email": "member@example.com", "safe": "ok"},
        },
        "breadcrumbs": [{"data": {"password": "hunter2"}}],
    }
    safe = sentry_before_send(event, {"exc_info": RuntimeError("raw")})
    assert safe["request"]["headers"]["Authorization"] == REDACTED
    assert safe["request"]["headers"]["Cookie"] == REDACTED
    assert safe["request"]["data"]["email"] == REDACTED
    assert safe["request"]["data"]["safe"] == "ok"
    assert safe["breadcrumbs"][0]["data"]["password"] == REDACTED


def test_request_authority_context_never_guesses_multi_branch() -> None:
    one = SimpleNamespace(
        state=SimpleNamespace(
            request_id="r-1",
            correlation_id="c-1",
            org_id="tenant-1",
            staff_id="principal-1",
            principal_type="staff",
            branch_ids=["branch-1"],
            otel_trace_id="trace-1",
            otel_span_id="span-1",
        )
    )
    many = SimpleNamespace(
        state=SimpleNamespace(
            request_id="r-2",
            correlation_id="c-2",
            org_id="tenant-2",
            staff_id="principal-2",
            principal_type="staff",
            branch_ids=["branch-1", "branch-2"],
        )
    )
    assert request_authority_context(one)["branch_id"] == "branch-1"
    assert request_authority_context(many)["branch_id"] == UNKNOWN


class _TrustedStateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.org_id = "tenant-1"
        request.state.staff_id = "principal-1"
        request.state.principal_type = "staff"
        request.state.branch_ids = ["branch-1"]
        request.state.otel_trace_id = "trace-1"
        request.state.otel_span_id = "span-1"
        return await call_next(request)


@pytest.mark.asyncio
async def test_request_middleware_sanitizes_correlation_and_binds_authoritative_context() -> None:
    async def context_endpoint(_: Request):
        return JSONResponse(current_observability_context())

    app = Starlette(routes=[Route("/context", context_endpoint)])
    app.add_middleware(AuthenticatedObservabilityContextMiddleware)
    app.add_middleware(_TrustedStateMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestObservabilityMiddleware)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/context", headers={"X-Request-ID": "bad correlation id"})

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] != UNKNOWN
    assert body["correlation_id"] == body["request_id"]
    assert response.headers["X-Request-ID"] == body["correlation_id"]
    assert response.headers["X-Doers-Request-ID"] == body["request_id"]
    assert body["tenant_id"] == "tenant-1"
    assert body["branch_id"] == "branch-1"
    assert body["principal_id"] == "principal-1"
    assert body["principal_type"] == "staff"
    assert body["trace_id"] == "trace-1"
    assert body["span_id"] == "span-1"
    assert all(value == UNKNOWN for value in current_observability_context().values())


def test_celery_context_propagates_bounded_headers_and_resets_after_task() -> None:
    publisher_tokens = bind_observability_context(
        request_id="request-1",
        correlation_id="corr-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        saga_id="saga-1",
    )
    headers: dict[str, object] = {}
    try:
        celery_context.inject_observability_headers(headers=headers)
    finally:
        reset_observability_context(publisher_tokens)

    inherited = headers["doers_observability"]
    assert inherited == {
        "request_id": "request-1",
        "correlation_id": "corr-1",
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "saga_id": "saga-1",
    }

    task = SimpleNamespace(
        name="app.tasks.test",
        request=SimpleNamespace(headers=headers),
    )
    celery_context.bind_task_observability_context(task_id="task-1", task=task, sender=task)
    active = current_observability_context()
    assert active["task_id"] == "task-1"
    assert active["correlation_id"] == "corr-1"
    assert active["tenant_id"] == "tenant-1"
    assert active["saga_id"] == "saga-1"

    celery_context.reset_task_observability_context(
        task_id="task-1",
        task=task,
        sender=task,
        state="SUCCESS",
    )
    assert all(value == UNKNOWN for value in current_observability_context().values())


def test_main_and_celery_wiring_preserve_p7_p6_boundaries() -> None:
    assert "configure_structured_logging(settings.LOG_LEVEL)" in MAIN_SOURCE
    assert "app.add_middleware(AuthenticatedObservabilityContextMiddleware)" in MAIN_SOURCE
    assert "app.add_middleware(RequestObservabilityMiddleware)" in MAIN_SOURCE
    assert MAIN_SOURCE.index("app.add_middleware(AuthenticatedObservabilityContextMiddleware)") < MAIN_SOURCE.index(
        "app.add_middleware(TenantMiddleware)"
    )
    assert MAIN_SOURCE.index("app.add_middleware(DrainAdmissionMiddleware)") < MAIN_SOURCE.index(
        "app.add_middleware(RequestObservabilityMiddleware)"
    )
    assert MAIN_SOURCE.index("app.add_middleware(RequestObservabilityMiddleware)") < MAIN_SOURCE.index(
        "app.add_middleware(SystemControlMiddleware)"
    )
    assert "task_acks_late=True" in CELERY_SOURCE
    assert "task_reject_on_worker_lost=True" in CELERY_SOURCE
    assert "worker_prefetch_multiplier=1" in CELERY_SOURCE
    assert "from app.observability import celery_context as _p8_celery_observability" in CELERY_SOURCE
