from __future__ import annotations

from app.platform_billing.domain.capability import (
    CapabilityDefinition,
    CapacityEffect,
    OperationClass,
)
from app.platform_billing.domain.capability_decision import (
    CapabilityDecision,
    CapabilityDecisionCode,
    CapabilityDecisionInput,
    CapabilityEntitlementValue,
    CapabilityUsageValue,
)
from app.platform_billing.policies.capability_registry import get_capability_registry


def resolve_capability_decision(inputs: CapabilityDecisionInput) -> CapabilityDecision:
    registry = get_capability_registry()
    capability = registry.get(inputs.capability_key)
    if capability is None:
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.PLATFORM_CAPABILITY_UNKNOWN,
            reason="capability_unknown",
            capability=None,
        )

    if inputs.operation_class != capability.operation_class.value:
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.PLATFORM_CAPABILITY_UNKNOWN,
            reason="operation_class_mismatch",
            capability=capability,
        )

    if inputs.unsupported_addon_composition:
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.ACCESS_DECISION_UNAVAILABLE,
            reason="unsupported_addon_composition",
            capability=capability,
        )

    if inputs.projection_freshness == "invalid_ahead":
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.PLATFORM_PROJECTION_INVALID,
            reason="projection_invalid_ahead",
            capability=capability,
        )

    if inputs.projection_freshness in {"stale_behind", "missing"} and not inputs.fallback_used:
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.ACCESS_DECISION_UNAVAILABLE,
            reason="projection_unavailable",
            capability=capability,
        )

    if inputs.fallback_used and not _fallback_permitted(capability):
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.ACCESS_DECISION_UNAVAILABLE,
            reason="fallback_not_permitted",
            capability=capability,
        )

    if inputs.access_mode not in capability.allowed_access_modes:
        return _decision(
            inputs,
            allowed=False,
            code=CapabilityDecisionCode.PLATFORM_ACCESS_DENIED,
            reason="access_mode_denied",
            capability=capability,
        )

    entitlement = _find_entitlement(inputs.entitlements, capability.required_feature_key)
    if capability.required_feature_key is not None:
        entitlement_decision = _evaluate_entitlement(capability, entitlement)
        if entitlement_decision is not None:
            return _decision(
                inputs,
                allowed=False,
                code=entitlement_decision,
                reason="entitlement_required",
                capability=capability,
                entitlement=entitlement,
            )

    if capability.capacity_effect == CapacityEffect.increase:
        usage = _find_usage(inputs.usage, capability.usage_metric_key)
        if usage is None or entitlement is None or not isinstance(entitlement.value, int):
            return _decision(
                inputs,
                allowed=False,
                code=CapabilityDecisionCode.ACCESS_DECISION_UNAVAILABLE,
                reason="usage_decision_unavailable",
                capability=capability,
                entitlement=entitlement,
                usage=usage,
            )
        if usage.current_value >= entitlement.value:
            return _decision(
                inputs,
                allowed=False,
                code=CapabilityDecisionCode.PLATFORM_USAGE_LIMIT_REACHED,
                reason="usage_limit_reached",
                capability=capability,
                entitlement=entitlement,
                usage=usage,
            )
        return _decision(
            inputs,
            allowed=True,
            code=CapabilityDecisionCode.ALLOWED,
            reason="usage_below_limit",
            capability=capability,
            entitlement=entitlement,
            usage=usage,
        )

    return _decision(
        inputs,
        allowed=True,
        code=CapabilityDecisionCode.ALLOWED,
        reason="capability_allowed",
        capability=capability,
        entitlement=entitlement,
    )


def _fallback_permitted(capability: CapabilityDefinition) -> bool:
    if not capability.fallback_eligible:
        return False
    return capability.operation_class in {
        OperationClass.safe_read,
        OperationClass.billing_recovery,
    }


def _evaluate_entitlement(
    capability: CapabilityDefinition,
    entitlement: CapabilityEntitlementValue | None,
) -> CapabilityDecisionCode | None:
    if entitlement is None:
        return CapabilityDecisionCode.PLATFORM_ENTITLEMENT_REQUIRED

    if isinstance(entitlement.value, bool):
        if entitlement.value is not True:
            return CapabilityDecisionCode.PLATFORM_ENTITLEMENT_REQUIRED
        return None

    if isinstance(entitlement.value, int) and not isinstance(entitlement.value, bool):
        if capability.capacity_effect == CapacityEffect.increase:
            return None
        if entitlement.value <= 0:
            return CapabilityDecisionCode.PLATFORM_ENTITLEMENT_REQUIRED
        return None

    return CapabilityDecisionCode.PLATFORM_ENTITLEMENT_REQUIRED


def _find_entitlement(
    entitlements: tuple[CapabilityEntitlementValue, ...],
    feature_key: str | None,
) -> CapabilityEntitlementValue | None:
    if feature_key is None:
        return None
    for entitlement in entitlements:
        if entitlement.feature_key == feature_key:
            return entitlement
    return None


def _find_usage(
    usage_values: tuple[CapabilityUsageValue, ...],
    metric_key: str | None,
) -> CapabilityUsageValue | None:
    if metric_key is None:
        return None
    for usage in usage_values:
        if usage.metric_key == metric_key:
            return usage
    return None


def _decision(
    inputs: CapabilityDecisionInput,
    *,
    allowed: bool,
    code: CapabilityDecisionCode,
    reason: str,
    capability: CapabilityDefinition | None,
    entitlement: CapabilityEntitlementValue | None = None,
    usage: CapabilityUsageValue | None = None,
) -> CapabilityDecision:
    return CapabilityDecision(
        allowed=allowed,
        decision_code=code.value,
        safe_reason_code=reason,
        capability_key=inputs.capability_key,
        operation_class=inputs.operation_class,
        access_mode=inputs.access_mode,
        required_feature_key=capability.required_feature_key if capability else None,
        entitlement_value=entitlement.value if entitlement else None,
        usage_value=usage.current_value if usage else None,
        limit_value=entitlement.value if entitlement and isinstance(entitlement.value, int) else None,
        projection_freshness=inputs.projection_freshness,
        fallback_used=inputs.fallback_used,
        recompute_attempted=inputs.recompute_attempted,
        source_subscription_version=inputs.source_subscription_version,
        decision_timestamp=inputs.decision_timestamp,
    )
