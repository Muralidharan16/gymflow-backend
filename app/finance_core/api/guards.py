from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException, status


FINANCE_PAYMENT_API_ENABLED = False


def require_finance_payment_api_enabled() -> NoReturn:
    """Hard-disabled until a later explicit public-route authorization.

    Keeping this as a dependency makes every Phase 6H route reject before
    service calls, payment mutation, provider behavior, or customer-facing
    effects can occur.
    """

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "FINANCE_PAYMENT_API_DISABLED",
            "message": "Finance payment API is disabled.",
        },
    )
