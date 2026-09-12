"""Public service exports without eager application bootstrap side effects.

Importing a focused service module must not instantiate unrelated settings,
clients, schedulers, or security infrastructure.  Attribute access keeps the
historic ``from app.services import X`` API while loading only the requested
service.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AuthService": ("app.services.auth_service", "AuthService"),
    "GymService": ("app.services.gym_service", "GymService"),
    "MemberService": ("app.services.member_service", "MemberService"),
    "SubscriptionService": ("app.services.subscription_service", "SubscriptionService"),
    "PaymentService": ("app.services.payment_service", "PaymentService"),
    "AttendanceService": ("app.services.attendance_service", "AttendanceService"),
    "InvoiceService": ("app.services.invoice_service", "InvoiceService"),
    "ImportService": ("app.services.import_service", "ImportService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
