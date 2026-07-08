from __future__ import annotations

from app.finance_core.domain.operational_guards import (
    FinanceKillSwitchStatus,
    FinanceOperationalGuardReport,
    FinanceOperationalPosture,
    validate_operational_posture,
)


class FinanceOperationalGuardService:
    def __init__(self, posture: FinanceOperationalPosture | None = None):
        self._posture = posture or FinanceOperationalPosture()

    def preflight(self) -> FinanceOperationalGuardReport:
        return validate_operational_posture(self._posture)

    def require_safe_preflight(self) -> FinanceOperationalGuardReport:
        report = self.preflight()
        if not report.safe:
            return report
        return report

    def kill_switch_status(self) -> FinanceKillSwitchStatus:
        report = self.preflight()
        return FinanceKillSwitchStatus(
            live_provider_actions_allowed=report.safe and report.live_provider_enabled,
            live_money_movement_allowed=report.safe and report.live_money_movement_enabled,
            subscription_automation_allowed=report.safe and report.subscription_automation_enabled,
            customer_facing_payment_allowed=report.safe and report.customer_facing_checkout_enabled,
        )
