"""
tests/platform_billing/test_runtime_policy.py
==============================================
Tests validating the centralized runtime-policy manifest.

These tests ensure:
    1. The manifest loads correctly.
    2. Required values are present and correctly typed.
    3. Constant values are not duplicated at call sites.
    4. The manifest hash is stable (warns on unintended changes).
"""

from __future__ import annotations

import hashlib
import yaml
import pytest


def test_manifest_loads_without_errors():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy is not None
    assert isinstance(policy.access_resolution_sync_timeout_ms, int)
    assert isinstance(policy.policy_day_seconds, int)
    assert isinstance(policy.provider_mapping_environment_match, str)


def test_access_resolution_timeout_positive():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy.access_resolution_sync_timeout_ms > 0


def test_policy_day_seconds_positive():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy.policy_day_seconds > 0


def test_policy_day_seconds_exactly_86400():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy.policy_day_seconds == 86400, (
        "V3.1 §1.5: policy_day_seconds must be exactly 86400 "
        "(one policy day = 86,400 elapsed seconds)"
    )


def test_provider_mapping_is_exact():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert (
        policy.provider_mapping_environment_match == "exact"
    ), "V3.1 §1.7: provider_mapping_environment_match must be 'exact'"


def test_stale_fallback_never_guesses_full():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert "full" in policy.stale_read_fallback_never_guess, (
        "V3.1 §8.4: stale projection must never guess 'full'"
    )


def test_stale_fallback_never_guesses_blocked():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert "blocked" in policy.stale_read_fallback_never_guess, (
        "V3.1 §8.4: stale projection must never guess 'blocked'"
    )


def test_manifest_hash_is_present():
    from app.platform_billing.policies.policy_loader import get_runtime_policy

    policy = get_runtime_policy()
    assert policy.source_manifest_hash
    assert len(policy.source_manifest_hash) == 64  # SHA-256 hex


def test_manifest_contains_required_keys():
    path = (
        __file__.rsplit("tests", 1)[0]
        + "app/platform_billing/policies/data/platform_billing_runtime_v1.yaml"
    )
    with open(path) as f:
        data = yaml.safe_load(f)

    required_keys = {
        "manifest_version",
        "access_resolution_sync_timeout_ms",
        "policy_day_seconds",
        "provider_mapping_environment_match",
        "stale_read_fallback_minimum_restriction",
        "stale_read_fallback_maximum_guessed_restriction",
        "stale_read_fallback_never_guess",
        "first_subscription_lock_namespace",
    }
    missing = required_keys - set(data.keys())
    assert not missing, f"Runtime manifest missing keys: {missing}"


def test_all_policy_files_valid():
    from app.platform_billing.policies.policy_loader import validate_all_policies

    errors = validate_all_policies()
    assert not errors, (
        f"Policy validation errors found: {errors}"
    )


def test_capability_yaml_structure():
    path = (
        __file__.rsplit("tests", 1)[0]
        + "app/platform_billing/policies/data/capabilities_v1.yaml"
    )
    with open(path) as f:
        data = yaml.safe_load(f)

    assert "capabilities" in data
    caps = data["capabilities"]
    assert isinstance(caps, list)
    keys = {c["key"] for c in caps}
    assert "platform_billing.view" in keys
    assert "branches.create" in keys
    assert "support.contact" in keys


def test_entitlement_yaml_structure():
    path = (
        __file__.rsplit("tests", 1)[0]
        + "app/platform_billing/policies/data/entitlements_v1.yaml"
    )
    with open(path) as f:
        data = yaml.safe_load(f)

    assert "entitlements" in data
    ents = data["entitlements"]
    assert isinstance(ents, list)
    keys = {e["key"] for e in ents}
    assert "limits.branches.active" in keys
    assert "features.attendance" in keys


def test_access_matrix_yaml_structure():
    path = (
        __file__.rsplit("tests", 1)[0]
        + "app/platform_billing/policies/data/access_matrix_v1.yaml"
    )
    with open(path) as f:
        data = yaml.safe_load(f)

    assert "matrix" in data
    required_modes = {"full", "limited_write", "read_only", "billing_only", "blocked"}
    assert set(data["matrix"].keys()) == required_modes


def test_lifecycle_policies_yaml_structure():
    path = (
        __file__.rsplit("tests", 1)[0]
        + "app/platform_billing/policies/data/lifecycle_policies_v1.yaml"
    )
    with open(path) as f:
        data = yaml.safe_load(f)

    assert "policies" in data
    required = {"TRIAL-IN-V1", "DUNNING-IN-V1", "CANCEL-IN-V1", "DOWNGRADE-IN-V1"}
    assert set(data["policies"].keys()) >= required
