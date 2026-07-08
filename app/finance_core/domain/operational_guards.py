from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FinanceSafetyMode = Literal["internal_only", "sandbox", "test"]


class FinanceOperationalGuardError(Exception):
    pass


@dataclass(frozen=True)
class FinanceOperationalPosture:
    safety_mode: FinanceSafetyMode = "internal_only"
    live_provider_enabled: bool = False
    live_money_movement_enabled: bool = False
    subscription_automation_enabled: bool = False
    customer_facing_checkout_enabled: bool = False
    public_payment_routes_enabled: bool = False
    kill_switch_live_actions_disabled: bool = True


@dataclass(frozen=True)
class FinanceGuardFailure:
    code: str
    message: str


@dataclass(frozen=True)
class FinanceOperationalGuardReport:
    safe: bool
    safety_mode: str
    live_provider_enabled: bool
    live_money_movement_enabled: bool
    subscription_automation_enabled: bool
    customer_facing_checkout_enabled: bool
    public_payment_routes_enabled: bool
    kill_switch_live_actions_disabled: bool
    failures: tuple[FinanceGuardFailure, ...]


@dataclass(frozen=True)
class FinanceKillSwitchStatus:
    live_provider_actions_allowed: bool = False
    live_money_movement_allowed: bool = False
    subscription_automation_allowed: bool = False
    customer_facing_payment_allowed: bool = False


ALLOWED_SAFETY_MODES = {"internal_only", "sandbox", "test"}


def validate_operational_posture(posture: FinanceOperationalPosture) -> FinanceOperationalGuardReport:
    failures: list[FinanceGuardFailure] = []
    if posture.safety_mode not in ALLOWED_SAFETY_MODES:
        failures.append(
            FinanceGuardFailure(
                code="finance.safety_mode.not_allowed",
                message="Finance safety mode is not approved for live behavior.",
            )
        )
    if posture.live_provider_enabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.live_provider.disabled",
                message="Live provider adapters are disabled for the current finance phase.",
            )
        )
    if posture.live_money_movement_enabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.live_money_movement.disabled",
                message="Live payment or refund execution is disabled for the current finance phase.",
            )
        )
    if posture.subscription_automation_enabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.subscription_automation.disabled",
                message="Subscription activation or deactivation automation is disabled.",
            )
        )
    if posture.customer_facing_checkout_enabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.customer_facing_checkout.disabled",
                message="Customer-facing checkout is disabled for the current finance phase.",
            )
        )
    if posture.public_payment_routes_enabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.public_payment_routes.disabled",
                message="Public payment routes are disabled for the current finance phase.",
            )
        )
    if not posture.kill_switch_live_actions_disabled:
        failures.append(
            FinanceGuardFailure(
                code="finance.kill_switch.required",
                message="Live-action kill switch must remain disabled for live actions.",
            )
        )
    return FinanceOperationalGuardReport(
        safe=not failures,
        safety_mode=posture.safety_mode,
        live_provider_enabled=posture.live_provider_enabled,
        live_money_movement_enabled=posture.live_money_movement_enabled,
        subscription_automation_enabled=posture.subscription_automation_enabled,
        customer_facing_checkout_enabled=posture.customer_facing_checkout_enabled,
        public_payment_routes_enabled=posture.public_payment_routes_enabled,
        kill_switch_live_actions_disabled=posture.kill_switch_live_actions_disabled,
        failures=tuple(failures),
    )
