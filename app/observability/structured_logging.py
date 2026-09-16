"""Structured JSON logging with bounded context and pre-sink redaction."""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import datetime, timezone
from typing import Any

from app.observability.context import current_observability_context
from app.observability.redaction import redact_text, redact_value


_STANDARD_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class StructuredJsonFormatter(logging.Formatter):
    """Emit one JSON object per record without serializing raw sensitive values."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = "unformattable log message"

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(message),
            **current_observability_context(),
        }

        extra: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_RECORD_FIELDS:
                continue
            if key in payload:
                continue
            extra[key] = redact_value(value, key=key)
        if extra:
            payload["fields"] = extra

        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_logging_config(level: str) -> dict[str, Any]:
    normalized_level = str(level or "INFO").upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "doers_json": {"()": "app.observability.structured_logging.StructuredJsonFormatter"},
        },
        "handlers": {
            "doers_console": {
                "class": "logging.StreamHandler",
                "formatter": "doers_json",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "handlers": ["doers_console"],
            "level": normalized_level,
        },
        "loggers": {
            "app": {"handlers": [], "level": normalized_level, "propagate": True},
            "doers": {"handlers": [], "level": normalized_level, "propagate": True},
            "uvicorn": {"handlers": [], "level": normalized_level, "propagate": True},
            "uvicorn.error": {"handlers": [], "level": normalized_level, "propagate": True},
            "uvicorn.access": {"handlers": [], "level": normalized_level, "propagate": True},
        },
    }


def configure_structured_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(build_logging_config(level))
