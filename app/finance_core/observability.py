"""PAY-18 Finance observability helpers.

Finance logs never emit raw payment/customer/provider identifiers as
correlation fields. The process-local P8 request/task correlation is converted
to a deterministic, non-reversible Finance correlation using an HMAC keyed by
the application secret. This value is observability evidence only.
"""

from __future__ import annotations

import hashlib
import hmac

from app.core.config import settings
from app.observability.context import UNKNOWN, current_observability_context


def sanitized_finance_correlation() -> str:
    context = current_observability_context()
    raw = context.get("correlation_id", UNKNOWN)
    if raw == UNKNOWN:
        raw = context.get("request_id", UNKNOWN)
    if not raw or raw == UNKNOWN:
        return "fin-unknown"

    secret = str(settings.SECRET_KEY or "").encode("utf-8")
    if not secret:
        return "fin-unknown"

    digest = hmac.new(
        secret,
        str(raw).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"fin-{digest[:20]}"
