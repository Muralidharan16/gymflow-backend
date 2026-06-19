"""
tests/platform_billing/test_phase2_domain.py
=============================================
Phase 2 domain unit tests for entitlement resolver, access resolver,
freshness classification, state machine, and canonical hashing.

No database required. Pure domain logic tests.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.platform_billing.domain.hashing import (
    CanonicalSerializer,
    compute_input_hash,
    ENTITLEMENT_RESOLVER_VERSION,
    ACCESS_RESOLVER_VERSION,
)
from app.platform_billing.domain.state_machine import (
    validate_contract_transition,
    validate_change_transition,
    ALLOWED_CONTRACT_TRANSITIONS,
    ALLOWED_CHANGE_TRANSITIONS,
)
from app.platform_billing.domain.freshness import (
    classify_projection_freshness,
    resolve_safe_fallback,
    is_operation_safe_for_fallback,
    ProjectionFreshness,
)
from app.platform_billing.domain.entitlement_resolver import (
    FeatureDefinition,
    PlanEntitlement,
    SubscriptionItem,
    EntitlementOverride,
    EntitlementResolverInput,
    resolve_entitlements,
)
from app.platform_billing.domain.access_resolver import (
    AccessResolverInput,
    SubscriptionInput,
    SubscriptionPeriod,
    SecurityBlock,
    AccessOverrideInput,
    resolve_access,
)


# ──────────────────────────────────────────────────────────────────────────
# 1. Canonical Hashing
# ──────────────────────────────────────────────────────────────────────────

class TestCanonicalHashing:
    def test_deterministic_serialization(self):
        s1 = CanonicalSerializer.serialize({"b": 2, "a": 1})
        s2 = CanonicalSerializer.serialize({"a": 1, "b": 2})
        assert s1 == s2

    def test_enum_serialization(self):
        from enum import Enum
        class Color(Enum):
            RED = "red"
        s = CanonicalSerializer.serialize({"color": Color.RED})
        assert '"color":"red"' in s

    def test_datetime_serialization(self):
        dt = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        s = CanonicalSerializer.serialize({"dt": dt})
        assert "2026-06-15T10:00:00Z" in s

    def test_compute_input_hash_stable(self):
        data = {"org_id": "abc", "version": 1}
        h1 = compute_input_hash(ENTITLEMENT_RESOLVER_VERSION, data)
        h2 = compute_input_hash(ENTITLEMENT_RESOLVER_VERSION, data)
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_input_hash_changes_with_input(self):
        data1 = {"org_id": "abc", "version": 1}
        data2 = {"org_id": "abc", "version": 2}
        h1 = compute_input_hash(ENTITLEMENT_RESOLVER_VERSION, data1)
        h2 = compute_input_hash(ENTITLEMENT_RESOLVER_VERSION, data2)
        assert h1 != h2


# ──────────────────────────────────────────────────────────────────────────
# 2. State Machine
# ──────────────────────────────────────────────────────────────────────────

class TestStateMachine:
    def test_valid_contract_transition(self):
        assert validate_contract_transition(None, "trialing") is True
        assert validate_contract_transition("trialing", "active") is True
        assert validate_contract_transition("active", "past_due") is True
        assert validate_contract_transition("active", "cancel_scheduled") is True
        assert validate_contract_transition("cancel_scheduled", "active") is True

    def test_forbidden_contract_transition(self):
        with pytest.raises(ValueError, match="Forbidden"):
            validate_contract_transition("canceled", "active")
        with pytest.raises(ValueError, match="Forbidden"):
            validate_contract_transition("expired", "trialing")
        with pytest.raises(ValueError, match="Forbidden"):
            validate_contract_transition("active", "trialing")

    def test_valid_change_transition(self):
        assert validate_change_transition("requested", "validated") is True
        assert validate_change_transition("validated", "provider_pending") is True
        assert validate_change_transition("scheduled", "applied") is True

    def test_forbidden_change_transition(self):
        with pytest.raises(ValueError, match="Forbidden"):
            validate_change_transition("applied", "requested")
        with pytest.raises(ValueError, match="Forbidden"):
            validate_change_transition("failed_final", "requested")


# ──────────────────────────────────────────────────────────────────────────
# 3. Entitlement Resolver
# ──────────────────────────────────────────────────────────────────────────

class TestEntitlementResolver:
    def test_base_plan_entitlement(self):
        now = datetime.now(timezone.utc)
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(
                SubscriptionItem(
                    plan_version_id="pv-1",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="limits.branches.active", value_type="integer", value_integer=3),
                    ),
                    item_type="base_plan",
                    status="active",
                ),
            ),
            feature_definitions=(
                FeatureDefinition(key="limits.branches.active", value_type="integer", enforcement_mode="hard"),
            ),
            active_overrides=(),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert len(result.entitlements) == 1
        assert result.entitlements[0].value_integer == 3

    def test_default_deny_for_missing_entitlement(self):
        now = datetime.now(timezone.utc)
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(),
            feature_definitions=(
                FeatureDefinition(key="features.multi_branch", value_type="boolean", enforcement_mode="hard"),
            ),
            active_overrides=(),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert result.entitlements[0].value_boolean is False
        assert "default: deny/zero" in str(result.entitlements[0].warnings)

    def test_override_wins(self):
        now = datetime.now(timezone.utc)
        override = EntitlementOverride(
            feature_key="limits.branches.active",
            value_json={"value": 10, "value_type": "integer"},
            status="active",
            starts_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
        )
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(
                SubscriptionItem(
                    plan_version_id="pv-1",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="limits.branches.active", value_type="integer", value_integer=3),
                    ),
                    item_type="base_plan",
                    status="active",
                ),
            ),
            feature_definitions=(
                FeatureDefinition(key="limits.branches.active", value_type="integer", enforcement_mode="hard"),
            ),
            active_overrides=(override,),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert result.entitlements[0].value_integer == 10

    def test_override_wrong_value_type_rejected(self):
        now = datetime.now(timezone.utc)
        override = EntitlementOverride(
            feature_key="limits.branches.active",
            value_json={"value": True, "value_type": "boolean"},
            status="active",
            starts_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
        )
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(),
            feature_definitions=(
                FeatureDefinition(key="limits.branches.active", value_type="integer", enforcement_mode="hard"),
            ),
            active_overrides=(override,),
            decision_timestamp=now,
            resolution_version=1,
        )
        with pytest.raises(ValueError, match="value_type mismatch"):
            resolve_entitlements(inputs)

    def test_integer_addon_does_not_compose_without_policy_rule(self):
        now = datetime.now(timezone.utc)
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(
                SubscriptionItem(
                    plan_version_id="pv-base",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="limits.branches.active", value_type="integer", value_integer=3),
                    ),
                    item_type="base_plan",
                    status="active",
                ),
                SubscriptionItem(
                    plan_version_id="pv-addon",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="limits.branches.active", value_type="integer", value_integer=2),
                    ),
                    item_type="addon",
                    status="active",
                ),
            ),
            feature_definitions=(
                FeatureDefinition(key="limits.branches.active", value_type="integer", enforcement_mode="hard"),
            ),
            active_overrides=(),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert result.entitlements[0].value_integer == 3
        assert result.entitlements[0].warnings == ("unsupported_addon_composition",)
        assert result.warnings == ("unsupported_addon_composition",)

    def test_boolean_addon_does_not_enable_without_policy_rule(self):
        now = datetime.now(timezone.utc)
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(
                SubscriptionItem(
                    plan_version_id="pv-addon",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="features.advanced_reports", value_type="boolean", value_boolean=True),
                    ),
                    item_type="addon",
                    status="active",
                ),
            ),
            feature_definitions=(
                FeatureDefinition(key="features.advanced_reports", value_type="boolean", enforcement_mode="hard"),
            ),
            active_overrides=(),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert result.entitlements[0].value_boolean is False
        assert result.entitlements[0].warnings == (
            "unsupported_addon_composition",
            "default: deny/zero",
        )

    def test_expired_override_ignored(self):
        now = datetime.now(timezone.utc)
        override = EntitlementOverride(
            feature_key="limits.branches.active",
            value_json={"value": 10, "value_type": "integer"},
            status="active",
            starts_at=now - timedelta(hours=3),
            expires_at=now - timedelta(hours=1),
        )
        inputs = EntitlementResolverInput(
            subscription_id="sub-1",
            subscription_version=1,
            active_items=(
                SubscriptionItem(
                    plan_version_id="pv-1",
                    plan_entitlements=(
                        PlanEntitlement(feature_key="limits.branches.active", value_type="integer", value_integer=3),
                    ),
                    item_type="base_plan",
                    status="active",
                ),
            ),
            feature_definitions=(
                FeatureDefinition(key="limits.branches.active", value_type="integer", enforcement_mode="hard"),
            ),
            active_overrides=(override,),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_entitlements(inputs)
        assert result.entitlements[0].value_integer == 3  # Base plan wins

    def test_deterministic_hashing(self):
        now = datetime.now(timezone.utc)
        inputs1 = EntitlementResolverInput(
            subscription_id="sub-1", subscription_version=1, active_items=(),
            feature_definitions=(
                FeatureDefinition(key="features.multi_branch", value_type="boolean", enforcement_mode="hard"),
            ),
            active_overrides=(), decision_timestamp=now, resolution_version=1,
        )
        inputs2 = EntitlementResolverInput(
            subscription_id="sub-1", subscription_version=1, active_items=(),
            feature_definitions=(
                FeatureDefinition(key="features.multi_branch", value_type="boolean", enforcement_mode="hard"),
            ),
            active_overrides=(), decision_timestamp=now, resolution_version=1,
        )
        r1 = resolve_entitlements(inputs1)
        r2 = resolve_entitlements(inputs2)
        assert r1.input_sha256 == r2.input_sha256


# ──────────────────────────────────────────────────────────────────────────
# 4. Access Resolver
# ──────────────────────────────────────────────────────────────────────────

class TestAccessResolver:
    def test_security_block_outranks_everything(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=None,
            security_block=SecurityBlock(active=True, reason="Security hold"),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "blocked"
        assert result.decision.reason_code == "SECURITY_SUSPENSION"

    def test_override_outranks_subscription(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=SubscriptionInput(
                id="sub-1", version=1, status="trialing",
                started_at=now - timedelta(days=1),
                current_period_start=now - timedelta(days=1),
                current_period_end=now + timedelta(days=13),
                cancel_at_period_end=False,
                periods=(SubscriptionPeriod(period_type="trial", starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=13), status="open"),),
            ),
            access_override=AccessOverrideInput(
                active=True, scope="access_mode", mode="read_only",
                reason="Support review", expires_at=now + timedelta(days=1),
            ),
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "read_only"

    def test_active_trial_full_access(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=SubscriptionInput(
                id="sub-1", version=1, status="trialing",
                started_at=now - timedelta(days=1),
                current_period_start=now - timedelta(days=1),
                current_period_end=now + timedelta(days=13),
                cancel_at_period_end=False,
                periods=(SubscriptionPeriod(period_type="trial", starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=13), status="open"),),
            ),
            has_valid_trial=True,
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "full"
        assert result.decision.reason_code == "TRIAL_ACTIVE"

    def test_no_service_period_billing_only(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=None,
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "billing_only"
        assert result.decision.reason_code == "NO_ACTIVE_SERVICE_PERIOD"

    def test_trial_grace_full_access(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=SubscriptionInput(
                id="sub-1", version=1, status="expired",
                started_at=now - timedelta(days=17),
                current_period_start=now - timedelta(days=17),
                current_period_end=now - timedelta(days=3),
                cancel_at_period_end=False,
                periods=(),
            ),
            within_trial_grace=True,
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "full"
        assert result.decision.reason_code == "TRIAL_GRACE"

    def test_read_only_window(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=None,
            within_read_only_window=True,
            read_only_reason="SUBSCRIPTION_EXPIRED",
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "read_only"

    def test_organization_closed(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=True,
            subscription=None,
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "billing_only"
        assert result.decision.reason_code == "ORGANIZATION_CLOSED"

    def test_inconsistent_state_billing_only(self):
        now = datetime.now(timezone.utc)
        inputs = AccessResolverInput(
            organization_id="org-1",
            organization_closed=False,
            subscription=None,
            state_inconsistent=True,
            decision_timestamp=now,
            resolution_version=1,
        )
        result = resolve_access(inputs)
        assert result.decision.mode == "billing_only"
        assert result.decision.reason_code == "BILLING_STATE_REVIEW_REQUIRED"


# ──────────────────────────────────────────────────────────────────────────
# 5. Freshness / Fallback
# ──────────────────────────────────────────────────────────────────────────

class TestFreshnessAndFallback:
    def test_fresh_equal_versions(self):
        result = classify_projection_freshness(3, 3)
        assert result.freshness == ProjectionFreshness.FRESH
        assert result.may_read is True
        assert result.privileged_write_allowed is True

    def test_stale_behind(self):
        result = classify_projection_freshness(5, 3)
        assert result.freshness == ProjectionFreshness.STALE_BEHIND
        assert result.may_read is False
        assert result.synchronous_recomputation_eligible is True
        assert result.safe_fallback_eligible is True
        assert result.privileged_write_allowed is False

    def test_invalid_ahead(self):
        result = classify_projection_freshness(3, 5)
        assert result.freshness == ProjectionFreshness.INVALID_AHEAD
        assert result.may_read is False
        assert result.privileged_write_allowed is False

    def test_missing_projection(self):
        result = classify_projection_freshness(1, None)
        assert result.freshness == ProjectionFreshness.MISSING

    def test_no_subscription_exists(self):
        result = classify_projection_freshness(None, 0)
        assert result.freshness == ProjectionFreshness.FRESH

    def test_fallback_full_to_read_only(self):
        mode, reason = resolve_safe_fallback("full")
        assert mode == "read_only"
        assert reason == "STALE_PROJECTION_FALLBACK"

    def test_fallback_billing_only_stays_billing_only(self):
        mode, reason = resolve_safe_fallback("billing_only")
        assert mode == "billing_only"

    def test_fallback_no_last_decision_is_read_only(self):
        mode, reason = resolve_safe_fallback(None)
        assert mode == "read_only"

    def test_fallback_never_guesses_full(self):
        for last in ("read_only", "limited_write", "blocked", "billing_only", None):
            mode, _ = resolve_safe_fallback(last)
            assert mode != "full", f"Fallback from {last} should not guess full"

    def test_fallback_never_guesses_blocked_without_block(self):
        mode, _ = resolve_safe_fallback("blocked")
        assert mode == "billing_only", "blocked without confirmed block → billing_only"

    def test_confirmed_block_remains_blocked(self):
        mode, reason = resolve_safe_fallback("full", has_confirmed_security_block=True)
        assert mode == "blocked"
        assert reason == "SECURITY_SUSPENSION"

    def test_unsafe_operations_not_eligible_for_fallback(self):
        assert is_operation_safe_for_fallback("financial", "platform_billing.view") is False
        assert is_operation_safe_for_fallback("destructive", "branches.delete") is False
        assert is_operation_safe_for_fallback("increase_capacity", "branches.create") is False
        assert is_operation_safe_for_fallback("internal", "internal.platform_billing.view") is False
        assert is_operation_safe_for_fallback("read", "branches.view") is True
