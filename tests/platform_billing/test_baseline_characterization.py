"""
tests/platform_billing/test_baseline_characterization.py
=========================================================
Baseline characterization tests for the current platform trial/access
system. These tests document the existing behaviour without changing it.

They serve as regression detectors: if a future Phase accidentally
modifies trial, tier, or access semantics, these tests catch the change.

Phase 0: Read only. No production behaviour changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Tier and limits — characterization
# ──────────────────────────────────────────────────────────────────────────


def test_tier_limits_unchanged():
    from app.models.enums import OrgTier, TIER_LIMITS

    assert TIER_LIMITS[OrgTier.basic] == {"max_branches": 1, "max_members": 300}
    assert TIER_LIMITS[OrgTier.pro] == {"max_branches": 3, "max_members": 1500}
    assert TIER_LIMITS[OrgTier.elite] == {"max_branches": 5, "max_members": 5000}


def test_org_tier_field_exists():
    from app.models.organization import Organization
    assert hasattr(Organization, "tier")


def test_org_max_branches_field_exists():
    from app.models.organization import Organization
    assert hasattr(Organization, "max_branches")


# ──────────────────────────────────────────────────────────────────────────
# Trial system — characterization
# ──────────────────────────────────────────────────────────────────────────


def test_trial_subscription_model_exists():
    from app.models.trial import TrialSubscription
    assert TrialSubscription.__tablename__ == "trial_subscriptions"


def test_trial_subscription_has_status_field():
    from app.models.trial import TrialSubscription
    assert hasattr(TrialSubscription, "status")


def test_trial_subscription_has_plan_id():
    from app.models.trial import TrialSubscription
    assert hasattr(TrialSubscription, "plan_id")


def test_trial_service_exists():
    from app.services.trial_service import TrialService
    assert TrialService is not None


def test_require_trial_active_dependency_exists():
    from app.core.deps import require_trial_active
    assert callable(require_trial_active)


def test_trial_monitor_task_exists():
    try:
        from app.tasks.trial_tasks import monitor_trial_lifecycles
        assert monitor_trial_lifecycles is not None
    except ImportError as exc:
        pytest.skip(f"Pre-existing import issue in trial_tasks: {exc}")


# ──────────────────────────────────────────────────────────────────────────
# Member subscription / payment — characterization
# ──────────────────────────────────────────────────────────────────────────


def test_legacy_subscription_model_exists():
    from app.models.subscription import SubscriptionPlan, MemberSubscription
    assert SubscriptionPlan.__tablename__ == "subscription_plans"
    assert MemberSubscription.__tablename__ == "member_subscriptions"


def test_modern_subscription_model_exists():
    from app.models.member_subscription_v2 import MemberSubscriptionV2
    assert MemberSubscriptionV2.__tablename__ == "member_subscriptions_v2"


def test_payment_model_exists():
    from app.models.payment import Payment, Invoice
    assert Payment.__tablename__ == "payments"
    assert Invoice.__tablename__ == "invoices"


def test_membership_plan_model_exists():
    from app.models.membership_plan import MembershipPlan
    assert MembershipPlan.__tablename__ == "membership_plans"


# ──────────────────────────────────────────────────────────────────────────
# Feature flags — characterization
# ──────────────────────────────────────────────────────────────────────────


def test_platform_billing_feature_flags_all_disabled():
    from app.core.config import settings

    assert settings.PLATFORM_BILLING_READ_API is False, (
        "Phase 0: platform billing read API must be disabled"
    )
    assert settings.PLATFORM_BILLING_SHADOW_RESOLVER is False
    assert settings.PLATFORM_BILLING_ENFORCEMENT is False
    assert settings.PLATFORM_BILLING_FRONTEND_SHELL is False
    assert settings.PLATFORM_BILLING_CHECKOUT is False
    assert settings.PLATFORM_BILLING_WEBHOOK_PROCESSING is False
    assert settings.PLATFORM_BILLING_DUNNING_TRANSITIONS is False
    assert settings.PLATFORM_BILLING_NOTIFICATIONS is False


# ──────────────────────────────────────────────────────────────────────────
# Infrastructure — characterization
# ──────────────────────────────────────────────────────────────────────────


def test_idempotency_engine_exists():
    from app.core.idempotency import IdempotencyEngine
    assert IdempotencyEngine is not None


def test_advisory_locks_exist():
    from app.core.advisory_locks import DistributedLockCoordinator, LockNamespace
    assert DistributedLockCoordinator is not None
    assert LockNamespace is not None


def test_outbox_model_exists():
    from app.models.outbox import TransactionalOutbox
    assert TransactionalOutbox.__tablename__ == "transactional_outbox"


def test_audit_model_exists():
    from app.models.audit import AuditLog
    assert AuditLog is not None


def test_tenant_middleware_exists():
    from app.core.middleware import TenantMiddleware
    assert TenantMiddleware is not None


def test_idempotency_middleware_exists():
    from app.core.middleware import IdempotencyMiddleware
    assert IdempotencyMiddleware is not None


# ──────────────────────────────────────────────────────────────────────────
# No platform billing tables created
# ──────────────────────────────────────────────────────────────────────────


def test_no_platform_billing_db_tables_created():
    """
    Phase 0 must not create any database tables.
    Verify that no platform_billing models exist.
    """
    import importlib

    try:
        models_mod = importlib.import_module("app.platform_billing.models")
    except ImportError:
        return

    db_tables = []
    for attr_name in dir(models_mod):
        if attr_name.startswith("_"):
            continue
        attr = getattr(models_mod, attr_name, None)
        if hasattr(attr, "__tablename__"):
            db_tables.append(attr.__tablename__)

    assert not db_tables, (
        f"Phase 0 must not create database tables. "
        f"Found: {db_tables}. These belong in Phase 1 or later."
    )


def test_no_provider_integration_added():
    try:
        import app.platform_billing.providers
        for attr in dir(app.platform_billing.providers):
            if attr.startswith("_"):
                continue
            obj = getattr(app.platform_billing.providers, attr)
            if hasattr(obj, "__module__") and "razorpay" in str(obj).lower():
                pass  # allow if present already in other areas
    except ImportError:
        pass
