"""Process-local observability context for requests, tasks and sagas.

The values in this module are evidence only. They never grant authorization and
must be populated from already-authoritative request/task state.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any, Mapping


UNKNOWN = "unknown"
_CONTEXT_FIELDS = (
    "request_id",
    "correlation_id",
    "tenant_id",
    "branch_id",
    "principal_id",
    "principal_type",
    "saga_id",
    "task_id",
    "trace_id",
    "span_id",
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")

_CONTEXT: dict[str, ContextVar[str]] = {
    name: ContextVar(f"doers_observability_{name}", default=UNKNOWN)
    for name in _CONTEXT_FIELDS
}


def normalize_identifier(value: Any, *, max_length: int = 256) -> str:
    """Return a bounded log-safe identifier or ``unknown``.

    This deliberately does not coerce arbitrary containers or free-form text into
    an identifier. It prevents caller-controlled request IDs from becoming log
    injection or unbounded-memory/cardinality inputs.
    """

    if value is None:
        return UNKNOWN
    text = str(value).strip()
    if not text or len(text) > max_length or not _IDENTIFIER_RE.fullmatch(text):
        return UNKNOWN
    return text


def current_observability_context() -> dict[str, str]:
    return {name: variable.get() for name, variable in _CONTEXT.items()}


def bind_observability_context(**values: Any) -> dict[str, Token[str]]:
    """Bind only known fields and return reset tokens for exact restoration."""

    unknown = set(values) - set(_CONTEXT_FIELDS)
    if unknown:
        raise KeyError(f"Unknown observability context fields: {sorted(unknown)}")

    tokens: dict[str, Token[str]] = {}
    for name, value in values.items():
        normalized = normalize_identifier(value)
        tokens[name] = _CONTEXT[name].set(normalized)
    return tokens


def reset_observability_context(tokens: Mapping[str, Token[str]]) -> None:
    for name in reversed(_CONTEXT_FIELDS):
        token = tokens.get(name)
        if token is not None:
            _CONTEXT[name].reset(token)


def request_authority_context(request: Any) -> dict[str, str]:
    """Extract observability fields only from state established by trusted middleware.

    A branch is emitted only when request state has an explicit branch ID or the
    validated JWT contains exactly one branch. Multiple authorized branches are
    intentionally represented as ``unknown`` rather than guessed.
    """

    state = request.state
    branch = getattr(state, "branch_id", None)
    if branch is None:
        branch_ids = getattr(state, "branch_ids", None)
        if isinstance(branch_ids, (list, tuple)) and len(branch_ids) == 1:
            branch = branch_ids[0]

    tenant = getattr(state, "org_id", None)
    if tenant is None:
        tenant = getattr(state, "gym_id", None)

    return {
        "request_id": normalize_identifier(getattr(state, "request_id", None)),
        "correlation_id": normalize_identifier(getattr(state, "correlation_id", None)),
        "tenant_id": normalize_identifier(tenant),
        "branch_id": normalize_identifier(branch),
        "principal_id": normalize_identifier(getattr(state, "staff_id", None)),
        "principal_type": normalize_identifier(getattr(state, "principal_type", None)),
        "saga_id": normalize_identifier(getattr(state, "saga_id", None)),
        "task_id": normalize_identifier(getattr(state, "task_id", None)),
        "trace_id": normalize_identifier(getattr(state, "otel_trace_id", None)),
        "span_id": normalize_identifier(getattr(state, "otel_span_id", None)),
    }


def propagatable_task_context() -> dict[str, str]:
    """Return the bounded context safe to copy into Celery message headers."""

    context = current_observability_context()
    return {
        key: context[key]
        for key in (
            "request_id",
            "correlation_id",
            "tenant_id",
            "branch_id",
            "principal_id",
            "principal_type",
            "saga_id",
            "trace_id",
            "span_id",
        )
        if context[key] != UNKNOWN
    }
