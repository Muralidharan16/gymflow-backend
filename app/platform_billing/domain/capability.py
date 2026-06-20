"""
Typed Platform Billing capability definitions.

This module contains schema objects only. The canonical capability data lives in
policies/data/capabilities_v1.yaml and is loaded through the policy loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OperationClass(str, Enum):
    safe_read = "safe_read"
    ordinary_write = "ordinary_write"
    capacity_increase = "capacity_increase"
    capacity_decrease = "capacity_decrease"
    financial = "financial"
    destructive = "destructive"
    export = "export"
    privileged_admin = "privileged_admin"
    billing_recovery = "billing_recovery"
    security_recovery = "security_recovery"
    internal = "internal"


class CapacityEffect(str, Enum):
    none = "none"
    increase = "increase"
    decrease = "decrease"


ACCESS_MODES = frozenset(
    {"full", "limited_write", "read_only", "billing_only", "blocked"}
)


@dataclass(frozen=True)
class CapabilityDefinition:
    key: str
    description: str
    operation_class: OperationClass
    allowed_access_modes: tuple[str, ...]
    required_feature_key: str | None = None
    usage_metric_key: str | None = None
    capacity_effect: CapacityEffect = CapacityEffect.none
    fallback_eligible: bool = False
    recovery_capability: bool = False
    internal: bool = False


@dataclass(frozen=True)
class CapabilityRegistry:
    manifest_version: str
    capabilities: tuple[CapabilityDefinition, ...]
    source_manifest_hash: str

    def get(self, key: str) -> CapabilityDefinition | None:
        for capability in self.capabilities:
            if capability.key == key:
                return capability
        return None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(cap.key for cap in self.capabilities)
