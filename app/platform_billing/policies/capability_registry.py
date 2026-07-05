from __future__ import annotations

import hashlib
from typing import Any

from app.platform_billing.domain.capability import (
    ACCESS_MODES,
    CapabilityDefinition,
    CapabilityRegistry,
    CapacityEffect,
    OperationClass,
)
from app.platform_billing.policies.policy_loader import POLICIES_DIR, _load_yaml
from app.platform_billing.services.usage_service import SUPPORTED_METRICS


_capability_registry: CapabilityRegistry | None = None


def get_capability_registry() -> CapabilityRegistry:
    global _capability_registry
    if _capability_registry is None:
        _capability_registry = load_capability_registry()
    return _capability_registry


def _reload_for_test() -> CapabilityRegistry:
    global _capability_registry
    _capability_registry = None
    return get_capability_registry()


def load_capability_registry() -> CapabilityRegistry:
    data = _load_yaml("capabilities_v1.yaml")
    entitlement_keys = _load_entitlement_keys()
    path = POLICIES_DIR / "capabilities_v1.yaml"
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    raw_capabilities = data.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise ValueError("capabilities_v1.yaml must define a capabilities list")

    seen: set[str] = set()
    capabilities: list[CapabilityDefinition] = []
    for raw in raw_capabilities:
        if not isinstance(raw, dict):
            raise ValueError("each capability must be a mapping")
        capability = _parse_capability(raw, entitlement_keys)
        if capability.key in seen:
            raise ValueError(f"duplicate capability key: {capability.key}")
        seen.add(capability.key)
        capabilities.append(capability)

    return CapabilityRegistry(
        manifest_version=str(data.get("manifest_version", "")),
        capabilities=tuple(capabilities),
        source_manifest_hash=source_hash,
    )


def _load_entitlement_keys() -> frozenset[str]:
    data = _load_yaml("entitlements_v1.yaml")
    raw = data.get("entitlements", [])
    if not isinstance(raw, list):
        raise ValueError("entitlements_v1.yaml must define an entitlements list")
    return frozenset(str(item["key"]) for item in raw if isinstance(item, dict) and "key" in item)


def _parse_capability(
    raw: dict[str, Any],
    entitlement_keys: frozenset[str],
) -> CapabilityDefinition:
    key = _required_string(raw, "key")
    if "/" in key or "{" in key or "}" in key:
        raise ValueError(f"capability key must not contain route path syntax: {key}")

    operation_class = OperationClass(_required_string(raw, "operation_class"))
    allowed_modes = raw.get("allowed_access_modes")
    if not isinstance(allowed_modes, list) or not allowed_modes:
        raise ValueError(f"capability {key} must declare allowed_access_modes")
    allowed_tuple = tuple(str(mode) for mode in allowed_modes)
    unknown_modes = set(allowed_tuple) - ACCESS_MODES
    if unknown_modes:
        raise ValueError(f"capability {key} declares unknown access modes: {sorted(unknown_modes)}")

    required_feature_key = _optional_string(raw, "required_feature_key")
    if required_feature_key is not None and required_feature_key not in entitlement_keys:
        raise ValueError(f"capability {key} references unknown feature key: {required_feature_key}")

    usage_metric_key = _optional_string(raw, "usage_metric_key")
    if usage_metric_key is not None and usage_metric_key not in SUPPORTED_METRICS:
        raise ValueError(f"capability {key} references unknown usage metric: {usage_metric_key}")

    capacity_effect = CapacityEffect(str(raw.get("capacity_effect", "none")))
    if operation_class == OperationClass.capacity_increase and capacity_effect != CapacityEffect.increase:
        raise ValueError(f"capacity-increasing capability {key} must declare capacity_effect=increase")
    if capacity_effect == CapacityEffect.increase and usage_metric_key is None:
        raise ValueError(f"capacity-increasing capability {key} must declare a usage_metric_key")

    return CapabilityDefinition(
        key=key,
        description=_required_string(raw, "description"),
        operation_class=operation_class,
        allowed_access_modes=allowed_tuple,
        required_feature_key=required_feature_key,
        usage_metric_key=usage_metric_key,
        capacity_effect=capacity_effect,
        fallback_eligible=bool(raw.get("fallback_eligible", False)),
        recovery_capability=bool(raw.get("recovery_capability", False)),
        internal=bool(raw.get("internal", False)),
    )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"capability missing non-empty {key}")
    return value.strip()


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"optional capability field {key} must be a non-empty string")
    return value.strip()
