"""
app/platform_billing/domain/entitlement_resolver.py
====================================================
Pure domain entitlement resolver for Platform Billing.

Computes resolved entitlement values from plan entitlements, overrides,
and feature definitions. No FastAPI, SQLAlchemy, Redis, Celery, or
provider dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.platform_billing.domain.hashing import (
    ENTITLEMENT_RESOLVER_VERSION,
    compute_input_hash,
)


class EntitlementValueType(str, Enum):
    boolean = "boolean"
    integer = "integer"
    string = "string"
    json_ = "json"


class EntitlementEnforcementMode(str, Enum):
    hard = "hard"
    soft = "soft"
    metered = "metered"
    informational = "informational"


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    value_type: str
    enforcement_mode: str
    default_value: Any = None
    safe_default: bool = False


@dataclass(frozen=True)
class PlanEntitlement:
    feature_key: str
    value_type: str
    source_plan_version_id: str | None = None
    value_boolean: bool | None = None
    value_integer: int | None = None
    value_string: str | None = None
    value_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class EntitlementOverride:
    feature_key: str
    value_json: dict[str, Any]
    status: str  # active | expired | revoked
    starts_at: datetime
    expires_at: datetime
    id: str | None = None


@dataclass(frozen=True)
class SubscriptionItem:
    plan_version_id: str
    plan_entitlements: tuple[PlanEntitlement, ...]
    item_type: str  # base_plan | addon
    status: str


@dataclass(frozen=True)
class EntitlementResolverInput:
    subscription_id: str | None
    subscription_version: int
    active_items: tuple[SubscriptionItem, ...]
    feature_definitions: tuple[FeatureDefinition, ...]
    active_overrides: tuple[EntitlementOverride, ...]
    decision_timestamp: datetime
    resolution_version: int


@dataclass(frozen=True)
class ResolvedEntitlement:
    feature_key: str
    value_type: str
    value_boolean: bool | None = None
    value_integer: int | None = None
    value_string: str | None = None
    value_json: dict[str, Any] | None = None
    source_plan_version_id: str | None = None
    source_override_id: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    resolution_version: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EntitlementResolverResult:
    entitlements: tuple[ResolvedEntitlement, ...]
    resolution_version: int
    input_sha256: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _get_value(
    entitlement: PlanEntitlement,
    value_type: str,
) -> Any:
    if value_type == "boolean":
        return entitlement.value_boolean
    if value_type == "integer":
        return entitlement.value_integer
    if value_type == "string":
        return entitlement.value_string
    if value_type == "json":
        return entitlement.value_json
    return None


def _get_feature_definition(
    feature_key: str,
    definitions: tuple[FeatureDefinition, ...],
) -> FeatureDefinition | None:
    for d in definitions:
        if d.key == feature_key:
            return d
    return None


def _value_columns(value_type: str, value: Any) -> dict[str, Any]:
    if value_type == "boolean" and isinstance(value, bool):
        return {"value_boolean": value}
    if value_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
        return {"value_integer": value}
    if value_type == "string" and isinstance(value, str):
        return {"value_string": value}
    if value_type == "json" and isinstance(value, dict):
        return {"value_json": value}
    raise ValueError(f"entitlement value does not match value_type {value_type!r}")


def _default_value_for(definition: FeatureDefinition) -> Any:
    if definition.safe_default:
        return definition.default_value
    if definition.value_type == "boolean":
        return False
    if definition.value_type == "integer":
        return 0
    if definition.value_type == "string":
        return ""
    if definition.value_type == "json":
        return {}
    raise ValueError(f"unsupported entitlement value_type {definition.value_type!r}")


def resolve_entitlements(
    inputs: EntitlementResolverInput,
) -> EntitlementResolverResult:
    """
    Resolve entitlements for an organization.

    Resolution order (V3.1 §9.2):
        1. active approved entitlement override
        2. active base-plan entitlement
        3. supported active add-on composition
        4. feature default (deny/zero unless safe)
    """
    input_sha256 = compute_input_hash(ENTITLEMENT_RESOLVER_VERSION, inputs)
    warnings: list[str] = []

    # Collect all base-plan entitlements
    base_plan_ents: dict[str, PlanEntitlement] = {}
    addon_ents: dict[str, list[PlanEntitlement]] = {}

    for item in inputs.active_items:
        for ent in item.plan_entitlements:
            if item.item_type == "base_plan":
                base_plan_ents[ent.feature_key] = ent
            elif item.item_type == "addon":
                addon_ents.setdefault(ent.feature_key, []).append(ent)

    # Build override lookup
    active_overrides_map: dict[str, EntitlementOverride] = {}
    for ovr in inputs.active_overrides:
        if ovr.status == "active":
            now = inputs.decision_timestamp
            if ovr.starts_at <= now < ovr.expires_at:
                active_overrides_map[ovr.feature_key] = ovr

    result: list[ResolvedEntitlement] = []

    for definition in inputs.feature_definitions:
        feature_key = definition.key
        warnings_for_feature: list[str] = []
        if addon_ents.get(feature_key):
            warnings_for_feature.append("unsupported_addon_composition")

        # 1. Override wins if active
        override = active_overrides_map.get(feature_key)
        if override is not None:
            ovr_val = override.value_json
            override_value = ovr_val.get("value")
            override_value_type = ovr_val.get("value_type")
            if override_value_type != definition.value_type:
                raise ValueError(
                    f"entitlement override value_type mismatch for {feature_key}: "
                    f"{override_value_type!r} != {definition.value_type!r}"
                )

            result.append(ResolvedEntitlement(
                feature_key=feature_key,
                value_type=definition.value_type,
                **_value_columns(definition.value_type, override_value),
                source_override_id=override.id,
                effective_from=override.starts_at,
                effective_until=override.expires_at,
                resolution_version=inputs.resolution_version,
                warnings=tuple(warnings_for_feature),
            ))
            warnings.extend(warnings_for_feature)
            continue

        # 2. Base plan entitlement
        base_ent = base_plan_ents.get(feature_key)
        if base_ent is not None:
            if base_ent.value_type != definition.value_type:
                raise ValueError(
                    f"plan entitlement value_type mismatch for {feature_key}: "
                    f"{base_ent.value_type!r} != {definition.value_type!r}"
                )
            result.append(ResolvedEntitlement(
                feature_key=feature_key,
                value_type=definition.value_type,
                **_value_columns(definition.value_type, _get_value(base_ent, definition.value_type)),
                source_plan_version_id=base_ent.source_plan_version_id,
                effective_from=inputs.decision_timestamp,
                resolution_version=inputs.resolution_version,
                warnings=tuple(warnings_for_feature),
            ))
            warnings.extend(warnings_for_feature)
            continue

        # 3. Add-on composition — not yet commercially approved
        #    V3.1 §11.4: no commercial composition rules exist in the policy registry.
        #    Without an approved composition rule, add-ons do not grant additional
        #    entitlement. The base-plan entitlement (or default) is used.
        #    The deterministic warning is emitted for observability.
        # 4. Default deny/zero
        default_val = _default_value_for(definition)
        default_warnings = tuple(
            warnings_for_feature
            + ([] if definition.safe_default else ["default: deny/zero"])
        )
        result.append(ResolvedEntitlement(
            feature_key=feature_key,
            value_type=definition.value_type,
            **_value_columns(definition.value_type, default_val),
            resolution_version=inputs.resolution_version,
            warnings=default_warnings,
        ))

        if warnings_for_feature:
            warnings.extend(warnings_for_feature)

    return EntitlementResolverResult(
        entitlements=tuple(result),
        resolution_version=inputs.resolution_version,
        input_sha256=input_sha256,
        warnings=tuple(warnings),
    )
