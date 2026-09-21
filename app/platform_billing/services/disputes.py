from __future__ import annotations

from dataclasses import dataclass

from app.platform_billing.domain.disputes import (
    CapturedPaymentTruth,
    DisputePlan,
    DisputeSnapshot,
    FinancialExceptionPlan,
    NormalizedFinancialFact,
    plan_financial_fact,
)


@dataclass(frozen=True)
class FinancialFactHandlingResult:
    plan: DisputePlan | FinancialExceptionPlan
    payment_status_rewrite: str | None = None


class FinancialExceptionPlannerService:
    """Provider-neutral PAY-13 planning boundary.

    Webhook and reconciliation adapters normalize provider facts before this
    service is invoked. The service never changes payment history; persistence
    is applied by the PAY-13 repository under database lifecycle constraints.
    """

    def plan(
        self,
        *,
        fact: NormalizedFinancialFact,
        payment: CapturedPaymentTruth | None,
        dispute: DisputeSnapshot | None,
    ) -> FinancialFactHandlingResult:
        return FinancialFactHandlingResult(
            plan=plan_financial_fact(
                fact=fact,
                payment=payment,
                dispute=dispute,
            ),
            payment_status_rewrite=None,
        )
