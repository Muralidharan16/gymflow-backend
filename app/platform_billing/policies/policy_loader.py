"""
app/platform_billing/policies/policy_loader.py
===============================================
Loads and validates version-controlled platform billing policy
files at application startup and in CI.

No production commercial values are hosted in code; those come
from an approved release manifest loaded separately.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

logger = logging.getLogger("doers.platform_billing.policies")

POLICIES_DIR = Path(__file__).resolve().parent / "data"

REQUIRED_POLICY_FILES = (
    "capabilities_v1.yaml",
    "entitlements_v1.yaml",
    "access_matrix_v1.yaml",
    "lifecycle_policies_v1.yaml",
    "platform_billing_runtime_v1.yaml",
)


@dataclass(frozen=True)
class DunningPolicyConfig:
    policy_code: str
    full_access_grace_days: int
    limited_write_stage_days: int
    read_only_stage_days: int
    final_mode: str
    max_attempts: int
    retry_spacing_hours: tuple[int, ...]
    provider_outage_counts_as_failure: bool
    mandate_unavailable_counts_as_failure: bool
    late_payment_recovers_access: bool
    supported_mandate_rails: tuple[str, ...]
    customer_notifications: tuple[str, ...]


@dataclass(frozen=True)
class RuntimePolicy:
    access_resolution_sync_timeout_ms: int
    policy_day_seconds: int
    provider_mapping_environment_match: str
    stale_read_fallback_minimum_restriction: str
    stale_read_fallback_maximum_guessed_restriction: str
    stale_read_fallback_never_guess: tuple[str, ...]
    first_subscription_lock_namespace: str
    source_manifest_hash: str

    @classmethod
    def load(cls) -> RuntimePolicy:
        return _load_and_validate_runtime_policy()


# Module-level singleton, validated at import time.
# Tests may override via _reload_for_test().
_runtime_policy: RuntimePolicy | None = None


def get_runtime_policy() -> RuntimePolicy:
    global _runtime_policy
    if _runtime_policy is None:
        _runtime_policy = RuntimePolicy.load()
    return _runtime_policy


def _reload_for_test() -> RuntimePolicy:
    global _runtime_policy
    _runtime_policy = None
    return get_runtime_policy()


def _load_yaml(name: str) -> dict[str, Any]:
    path = POLICIES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Required policy file missing: {path}")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"Policy file {name} must contain a YAML mapping")
    return data


def _load_and_validate_runtime_policy() -> RuntimePolicy:
    data = _load_yaml("platform_billing_runtime_v1.yaml")
    raw_hash = hashlib.sha256(
        (POLICIES_DIR / "platform_billing_runtime_v1.yaml")
        .read_bytes()
    ).hexdigest()

    access_timeout = int(data["access_resolution_sync_timeout_ms"])
    if access_timeout <= 0:
        raise ValueError("access_resolution_sync_timeout_ms must be positive")

    policy_day = int(data["policy_day_seconds"])
    if policy_day <= 0:
        raise ValueError("policy_day_seconds must be positive")

    env_match = str(data["provider_mapping_environment_match"])
    if env_match != "exact":
        raise ValueError(
            f"provider_mapping_environment_match must be 'exact', got {env_match!r}"
        )

    return RuntimePolicy(
        access_resolution_sync_timeout_ms=access_timeout,
        policy_day_seconds=policy_day,
        provider_mapping_environment_match=env_match,
        stale_read_fallback_minimum_restriction=str(
            data["stale_read_fallback_minimum_restriction"]
        ),
        stale_read_fallback_maximum_guessed_restriction=str(
            data["stale_read_fallback_maximum_guessed_restriction"]
        ),
        stale_read_fallback_never_guess=tuple(
            sorted(data.get("stale_read_fallback_never_guess", []))
        ),
        first_subscription_lock_namespace=str(
            data["first_subscription_lock_namespace"]
        ),
        source_manifest_hash=raw_hash,
    )



def get_dunning_policy_config() -> DunningPolicyConfig:
    data = _load_yaml("lifecycle_policies_v1.yaml")
    raw = data.get("policies", {}).get("DUNNING-IN-V1")
    if not isinstance(raw, dict):
        raise ValueError("DUNNING-IN-V1 policy is missing")
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ValueError("DUNNING-IN-V1 params must be a mapping")

    max_attempts = int(params["max_attempts"])
    retry_spacing = tuple(int(v) for v in params["retry_spacing_hours"])
    if max_attempts <= 0:
        raise ValueError("DUNNING-IN-V1 max_attempts must be positive")
    if len(retry_spacing) < max_attempts:
        raise ValueError("DUNNING-IN-V1 retry_spacing_hours must cover max_attempts")
    if tuple(sorted(retry_spacing)) != retry_spacing:
        raise ValueError("DUNNING-IN-V1 retry spacing must be non-decreasing")
    if bool(params["provider_outage_counts_as_failure"]):
        raise ValueError("PAY-12 forbids provider outage from counting as customer failure")

    rails = tuple(str(v) for v in params["supported_mandate_rails"])
    required_rails = {"upi_autopay", "e_mandate", "card_recurring"}
    if not required_rails.issubset(set(rails)):
        raise ValueError("DUNNING-IN-V1 is missing required recurring payment rails")

    return DunningPolicyConfig(
        policy_code="DUNNING-IN-V1",
        full_access_grace_days=int(params["full_access_grace_days"]),
        limited_write_stage_days=int(params["limited_write_stage_days"]),
        read_only_stage_days=int(params["read_only_stage_days"]),
        final_mode=str(params["final_mode"]),
        max_attempts=max_attempts,
        retry_spacing_hours=retry_spacing,
        provider_outage_counts_as_failure=bool(params["provider_outage_counts_as_failure"]),
        mandate_unavailable_counts_as_failure=bool(params["mandate_unavailable_counts_as_failure"]),
        late_payment_recovers_access=bool(params["late_payment_recovers_access"]),
        supported_mandate_rails=rails,
        customer_notifications=tuple(str(v) for v in params["customer_notifications"]),
    )

def validate_all_policies() -> dict[str, str]:
    errors: dict[str, str] = {}
    for filename in REQUIRED_POLICY_FILES:
        try:
            _load_yaml(filename)
        except Exception as exc:
            errors[filename] = str(exc)
    try:
        get_dunning_policy_config()
    except Exception as exc:
        errors["lifecycle_policies_v1.yaml"] = str(exc)
    try:
        from app.platform_billing.policies.capability_registry import load_capability_registry

        load_capability_registry()
    except Exception as exc:
        errors["capabilities_v1.yaml"] = str(exc)
    return errors
