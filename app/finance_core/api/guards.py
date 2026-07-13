from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from fastapi import Depends, HTTPException, status


FINANCE_PAYMENT_API_ENABLED = False


@dataclass(frozen=True)
class FinanceCheckoutRoutePosture:
    sandbox_checkout_enabled: bool = False
    provider_mode: str = "disabled"
    live_provider_enabled: bool = False
    live_money_movement_enabled: bool = False
    customer_facing_checkout_enabled: bool = False
    public_webhook_enabled: bool = False
    internal_payment_application_enabled: bool = False


@dataclass(frozen=True)
class FinanceWebhookRoutePosture:
    sandbox_webhook_enabled: bool = False
    provider_mode: str = "disabled"
    live_provider_enabled: bool = False
    live_money_movement_enabled: bool = False
    production_webhook_enabled: bool = False
    internal_payment_application_enabled: bool = False


def get_finance_webhook_route_posture() -> FinanceWebhookRoutePosture:
    return FinanceWebhookRoutePosture()


def get_finance_checkout_route_posture() -> FinanceCheckoutRoutePosture:
    return FinanceCheckoutRoutePosture()


def _raise_finance_payment_api_disabled() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "FINANCE_PAYMENT_API_DISABLED",
            "message": "Finance payment API is disabled.",
        },
    )


def require_finance_payment_api_enabled() -> NoReturn:
    """Hard-disabled until a later explicit public-route authorization.

    Keeping this as a dependency makes every Phase 6H route reject before
    service calls, payment mutation, provider behavior, or customer-facing
    effects can occur.
    """

    _raise_finance_payment_api_disabled()


def require_finance_checkout_sandbox_enabled(
    posture: FinanceCheckoutRoutePosture = Depends(get_finance_checkout_route_posture),
) -> None:
    """Allow only the explicit Phase 6N sandbox checkout route posture.

    This intentionally does not enable the broader payment API. Webhook,
    internal application, admin, and status routes continue using
    require_finance_payment_api_enabled and stay unreachable.
    """

    if not posture.sandbox_checkout_enabled:
        _raise_finance_payment_api_disabled()

    if (
        posture.provider_mode not in {"sandbox", "test"}
        or posture.live_provider_enabled
        or posture.live_money_movement_enabled
        or posture.customer_facing_checkout_enabled
        or posture.public_webhook_enabled
        or posture.internal_payment_application_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FINANCE_CHECKOUT_SANDBOX_POSTURE_UNSAFE",
                "message": "Finance checkout sandbox posture is unsafe.",
            },
        )

    return None


def require_finance_webhook_sandbox_enabled(
    posture: FinanceWebhookRoutePosture = Depends(get_finance_webhook_route_posture),
) -> None:
    """Allow only explicit Phase 6P sandbox webhook execution.

    Checkout keeps its separate Phase 6N guard. Internal apply, status,
    and admin routes continue using the hard-disabled payment API guard.
    """

    if not posture.sandbox_webhook_enabled:
        _raise_finance_payment_api_disabled()

    if (
        posture.provider_mode not in {"sandbox", "test"}
        or posture.live_provider_enabled
        or posture.live_money_movement_enabled
        or posture.production_webhook_enabled
        or posture.internal_payment_application_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FINANCE_WEBHOOK_SANDBOX_POSTURE_UNSAFE",
                "message": "Finance webhook sandbox posture is unsafe.",
            },
        )

    return None
