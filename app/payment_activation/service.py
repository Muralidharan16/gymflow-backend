from __future__ import annotations

import uuid
from dataclasses import replace

from app.payment_activation.domain import (
    ActivationCapability,
    ActivationDecision,
    ActivationRuntime,
    KillSwitches,
    ProductionActivationPolicy,
    validate_stage_transition,
)


class PaymentActivationService:
    """Legacy PAY-22 pure-policy convenience service.

    Instances contain process-local immutable values only.  They are retained
    for compatibility and must not be treated as PAY-24A durable authority or
    used as the provider-operation admission boundary.
    """

    def __init__(self, runtime: ActivationRuntime):
        self._runtime = runtime
        self._policy = ProductionActivationPolicy(runtime)

    @property
    def runtime(self) -> ActivationRuntime:
        return self._runtime

    def decision(
        self,
        capability: ActivationCapability,
        *,
        organization_id: uuid.UUID | None,
    ) -> ActivationDecision:
        return self._policy.decide(
            capability,
            organization_id=organization_id,
        )

    def with_kill_switches(self, kill_switches: KillSwitches) -> "PaymentActivationService":
        return PaymentActivationService(
            replace(self._runtime, kill_switches=kill_switches)
        )

    def with_provider_egress(self, enabled: bool) -> "PaymentActivationService":
        return PaymentActivationService(
            replace(self._runtime, provider_egress_enabled=enabled)
        )

    def transition(self, proposed_runtime: ActivationRuntime) -> "PaymentActivationService":
        decision = validate_stage_transition(self._runtime, proposed_runtime)
        if not decision.allowed:
            raise PaymentActivationTransitionDenied(decision.code)
        return PaymentActivationService(proposed_runtime)


class PaymentActivationTransitionDenied(RuntimeError):
    pass
