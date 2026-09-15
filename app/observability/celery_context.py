"""Celery observability context propagation without changing task semantics."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from celery.signals import (
    before_task_publish,
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
)

from app.core.config import settings
from app.observability.context import (
    UNKNOWN,
    bind_observability_context,
    normalize_identifier,
    propagatable_task_context,
    reset_observability_context,
)
from app.observability.structured_logging import configure_structured_logging


logger = logging.getLogger("doers.observability.celery")
_HEADER = "doers_observability"
_ALLOWED_INHERITED_FIELDS = {
    "request_id",
    "correlation_id",
    "tenant_id",
    "branch_id",
    "principal_id",
    "principal_type",
    "saga_id",
    "trace_id",
    "span_id",
}
_ACTIVE_TASK_TOKENS: dict[str, dict[str, Any]] = {}


@setup_logging.connect
def configure_celery_structured_logging(*args, **kwargs) -> None:
    del args, kwargs
    configure_structured_logging(settings.LOG_LEVEL)


@before_task_publish.connect
def inject_observability_headers(headers=None, **kwargs) -> None:
    del kwargs
    if not isinstance(headers, dict):
        return
    inherited = propagatable_task_context()
    if inherited:
        headers[_HEADER] = inherited


def _task_inherited_context(task: Any) -> dict[str, str]:
    request_headers = getattr(getattr(task, "request", None), "headers", None)
    if not isinstance(request_headers, Mapping):
        return {}
    raw = request_headers.get(_HEADER)
    if not isinstance(raw, Mapping):
        return {}

    inherited: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_INHERITED_FIELDS:
            continue
        normalized = normalize_identifier(value)
        if normalized != UNKNOWN:
            inherited[key] = normalized
    return inherited


def _task_token_key(task_id: Any, task: Any) -> str:
    normalized = normalize_identifier(task_id)
    if normalized != UNKNOWN:
        return normalized
    # Defensive fallback for direct unit invocation. Production Celery task IDs
    # are always present, but context must still be reset if a malformed signal
    # reaches this handler.
    return f"object-{id(task)}"


@task_prerun.connect
def bind_task_observability_context(task_id=None, task=None, sender=None, **kwargs) -> None:
    del kwargs
    normalized_task_id = normalize_identifier(task_id)
    values = _task_inherited_context(task)
    values["task_id"] = normalized_task_id
    tokens = bind_observability_context(**values)
    _ACTIVE_TASK_TOKENS[_task_token_key(task_id, task)] = tokens
    logger.info(
        "Celery task started",
        extra={
            "event": "celery.task.started",
            "task_name": getattr(sender, "name", None) or getattr(task, "name", "unknown"),
        },
    )


@task_failure.connect
def log_task_failure(task_id=None, exception=None, traceback=None, sender=None, **kwargs) -> None:
    del kwargs
    exc_info = None
    if exception is not None:
        exc_info = (type(exception), exception, traceback)
    logger.error(
        "Celery task failed",
        exc_info=exc_info,
        extra={
            "event": "celery.task.failed",
            "task_name": getattr(sender, "name", "unknown"),
            "failed_task_id": normalize_identifier(task_id),
        },
    )


@task_postrun.connect
def reset_task_observability_context(task_id=None, task=None, sender=None, state=None, **kwargs) -> None:
    del kwargs
    logger.info(
        "Celery task completed",
        extra={
            "event": "celery.task.completed",
            "task_name": getattr(sender, "name", None) or getattr(task, "name", "unknown"),
            "task_state": str(state or "unknown"),
        },
    )
    tokens = _ACTIVE_TASK_TOKENS.pop(_task_token_key(task_id, task), None)
    if tokens is not None:
        reset_observability_context(tokens)
