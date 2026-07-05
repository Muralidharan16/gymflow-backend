from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.deps import Staff
from app.platform_billing.api.dependencies import require_platform_capability
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.domain.capability_decision import (
    CapabilityDecisionInput,
    CapabilityEntitlementValue,
    CapabilityUsageValue,
)
from app.platform_billing.domain.capability_resolver import resolve_capability_decision
from app.platform_billing.models.audit import PlatformBillingAuditEvent
from app.platform_billing.policies.capability_registry import get_capability_registry
from app.platform_billing.policies import capability_registry
from app.platform_billing.services.capability_authorization_service import (
    CapabilityAuthorizationService,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    cleanup_phase1_tables,
    exec_sql,
    seed_billing_account_and_subscription,
    seed_organizations,
)


def _decision_input(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "organization_id": str(uuid4()),
        "capability_key": "branches.view",
        "operation_class": OperationClass.safe_read.value,
        "decision_timestamp": now,
        "access_mode": "full",
        "projection_freshness": "fresh",
        "entitlements": (),
        "usage": (),
        "fallback_used": False,
        "recompute_attempted": False,
        "source_subscription_version": 1,
    }
    values.update(overrides)
    return CapabilityDecisionInput(**values)


def test_capability_registry_loads_phase3_definitions():
    registry = get_capability_registry()
    create_branch = registry.get("branches.create")
    assert create_branch is not None
    assert create_branch.operation_class == OperationClass.capacity_increase
    assert create_branch.required_feature_key == "limits.branches.active"
    assert create_branch.usage_metric_key == "limits.branches.active"
    assert create_branch.allowed_access_modes == ("full",)


def test_capability_registry_hash_is_deterministic():
    first = capability_registry._reload_for_test()
    second = capability_registry._reload_for_test()
    assert first.source_manifest_hash == second.source_manifest_hash
    assert len(first.source_manifest_hash) == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("required_feature_key", "features.unknown", "unknown feature key"),
        ("usage_metric_key", "limits.unknown", "unknown usage metric"),
        ("allowed_access_modes", ["unknown"], "unknown access modes"),
    ],
)
def test_capability_registry_rejects_unknown_references(field, value, message):
    raw = {
        "key": "test.capability",
        "description": "Test capability",
        "operation_class": "safe_read",
        "allowed_access_modes": ["full"],
        "fallback_eligible": False,
        "recovery_capability": False,
    }
    raw[field] = value
    with pytest.raises(ValueError, match=message):
        capability_registry._parse_capability(raw, frozenset({"features.allowed"}))


def test_capability_registry_rejects_route_path_syntax_and_duplicate_keys(monkeypatch):
    with pytest.raises(ValueError, match="route path syntax"):
        capability_registry._parse_capability(
            {
                "key": "/gyms/{gym_id}",
                "description": "Route shaped key",
                "operation_class": "safe_read",
                "allowed_access_modes": ["full"],
            },
            frozenset(),
        )

    original = capability_registry._parse_capability
    parsed = original(
        {
            "key": "test.duplicate",
            "description": "Duplicate",
            "operation_class": "safe_read",
            "allowed_access_modes": ["full"],
        },
        frozenset(),
    )

    monkeypatch.setattr(
        capability_registry,
        "_load_yaml",
        lambda name: {"capabilities": [{}, {}]} if name == "capabilities_v1.yaml" else {"entitlements": []},
    )
    monkeypatch.setattr(
        capability_registry,
        "_parse_capability",
        lambda raw, entitlement_keys: parsed,
    )
    with pytest.raises(ValueError, match="duplicate capability key"):
        capability_registry.load_capability_registry()
    monkeypatch.setattr(capability_registry, "_parse_capability", original)


def test_safe_read_allowed_for_read_only_mode():
    decision = resolve_capability_decision(
        _decision_input(access_mode="read_only")
    )
    assert decision.allowed is True
    assert decision.decision_code == "ALLOWED"


def test_read_only_write_denied():
    decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.update",
            operation_class=OperationClass.ordinary_write.value,
            access_mode="read_only",
        )
    )
    assert decision.allowed is False
    assert decision.decision_code == "PLATFORM_ACCESS_DENIED"


def test_capacity_increase_below_limit_allowed():
    decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.create",
            operation_class=OperationClass.capacity_increase.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(
                CapabilityUsageValue(
                    metric_key="limits.branches.active",
                    current_value=2,
                ),
            ),
        )
    )
    assert decision.allowed is True
    assert decision.limit_value == 3
    assert decision.usage_value == 2


def test_capacity_increase_at_limit_denied():
    decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.create",
            operation_class=OperationClass.capacity_increase.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(
                CapabilityUsageValue(
                    metric_key="limits.branches.active",
                    current_value=3,
                ),
            ),
        )
    )
    assert decision.allowed is False
    assert decision.decision_code == "PLATFORM_USAGE_LIMIT_REACHED"


def test_capacity_increase_above_limit_denied():
    decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.create",
            operation_class=OperationClass.capacity_increase.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(
                CapabilityUsageValue(
                    metric_key="limits.branches.active",
                    current_value=4,
                ),
            ),
        )
    )
    assert decision.allowed is False
    assert decision.decision_code == "PLATFORM_USAGE_LIMIT_REACHED"


def test_capacity_decrease_allowed_while_over_limit():
    decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.change_status",
            operation_class=OperationClass.capacity_decrease.value,
            access_mode="limited_write",
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(
                CapabilityUsageValue(
                    metric_key="limits.branches.active",
                    current_value=4,
                ),
            ),
        )
    )
    assert decision.allowed is True


def test_missing_entitlement_denied_and_missing_usage_unavailable():
    missing_entitlement = resolve_capability_decision(
        _decision_input(
            capability_key="attendance.record",
            operation_class=OperationClass.ordinary_write.value,
            entitlements=(),
        )
    )
    assert missing_entitlement.allowed is False
    assert missing_entitlement.decision_code == "PLATFORM_ENTITLEMENT_REQUIRED"

    missing_usage = resolve_capability_decision(
        _decision_input(
            capability_key="branches.create",
            operation_class=OperationClass.capacity_increase.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(),
        )
    )
    assert missing_usage.allowed is False
    assert missing_usage.decision_code == "ACCESS_DECISION_UNAVAILABLE"
    assert missing_usage.usage_value is None


def test_boolean_feature_requires_explicit_true():
    false_decision = resolve_capability_decision(
        _decision_input(
            capability_key="attendance.record",
            operation_class=OperationClass.ordinary_write.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="features.attendance",
                    value_type="boolean",
                    value=False,
                ),
            ),
        )
    )
    assert false_decision.allowed is False
    assert false_decision.decision_code == "PLATFORM_ENTITLEMENT_REQUIRED"

    true_decision = resolve_capability_decision(
        _decision_input(
            capability_key="attendance.record",
            operation_class=OperationClass.ordinary_write.value,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="features.attendance",
                    value_type="boolean",
                    value=True,
                ),
            ),
        )
    )
    assert true_decision.allowed is True


def test_invalid_ahead_projection_is_integrity_failure():
    decision = resolve_capability_decision(
        _decision_input(projection_freshness="invalid_ahead")
    )
    assert decision.allowed is False
    assert decision.decision_code == "PLATFORM_PROJECTION_INVALID"


def test_safe_read_fallback_allowed_only_for_registered_safe_reads():
    decision = resolve_capability_decision(
        _decision_input(
            access_mode="read_only",
            projection_freshness="stale_behind",
            fallback_used=True,
        )
    )
    assert decision.allowed is True

    write_decision = resolve_capability_decision(
        _decision_input(
            capability_key="branches.update",
            operation_class=OperationClass.ordinary_write.value,
            access_mode="limited_write",
            projection_freshness="stale_behind",
            fallback_used=True,
        )
    )
    assert write_decision.allowed is False
    assert write_decision.decision_code == "ACCESS_DECISION_UNAVAILABLE"


@pytest.mark.parametrize(
    ("capability_key", "operation_class", "access_mode"),
        [
            ("data.export", OperationClass.export.value, "read_only"),
            ("staff.update", OperationClass.privileged_admin.value, "full"),
            ("branches.create", OperationClass.capacity_increase.value, "full"),
        ],
)
def test_fallback_rejected_for_writes_exports_admin_and_capacity(
    capability_key: str,
    operation_class: str,
    access_mode: str,
):
    decision = resolve_capability_decision(
        _decision_input(
            capability_key=capability_key,
            operation_class=operation_class,
            access_mode=access_mode,
            projection_freshness="stale_behind",
            fallback_used=True,
            entitlements=(
                CapabilityEntitlementValue(
                    feature_key="limits.branches.active",
                    value_type="integer",
                    value=3,
                ),
            ),
            usage=(
                CapabilityUsageValue(
                    metric_key="limits.branches.active",
                    current_value=1,
                ),
            ),
        )
    )
    assert decision.allowed is False
    assert decision.decision_code == "ACCESS_DECISION_UNAVAILABLE"


def test_access_modes_cover_full_limited_read_billing_and_blocked_security():
    limited_write = resolve_capability_decision(
        _decision_input(
            capability_key="branches.update",
            operation_class=OperationClass.ordinary_write.value,
            access_mode="limited_write",
        )
    )
    assert limited_write.allowed is True

    billing_recovery = resolve_capability_decision(
        _decision_input(
            capability_key="support.contact",
            operation_class=OperationClass.billing_recovery.value,
            access_mode="billing_only",
        )
    )
    assert billing_recovery.allowed is True

    blocked_support = resolve_capability_decision(
        _decision_input(
            capability_key="auth.session.refresh",
            operation_class=OperationClass.security_recovery.value,
            access_mode="blocked",
        )
    )
    assert blocked_support.allowed is True


def test_unsupported_addon_composition_fails_unavailable():
    decision = resolve_capability_decision(
        _decision_input(unsupported_addon_composition=True)
    )
    assert decision.allowed is False
    assert decision.decision_code == "ACCESS_DECISION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_dependency_flags_disabled_preserves_legacy_path(monkeypatch):
    from app.core.config import settings
    from app.platform_billing.services import capability_authorization_service as auth_service

    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", False)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", False)

    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("authorization service should not run when flags are disabled")

    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", fail_if_called)

    staff = Staff(id=uuid4(), org_id=uuid4(), gym_id=None, role="owner")
    dependency = require_platform_capability("branches.view", OperationClass.safe_read.value)
    context = await dependency(
        request=SimpleNamespace(state=SimpleNamespace(correlation_id="test")),
        staff=staff,
        db=object(),
    )
    assert context.staff == staff
    assert context.decision is None
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shadow", "enforcement", "expected_service_calls", "expected_block", "expected_context"),
    [
        (False, False, 0, False, (False, False)),
        (True, False, 1, False, (True, False)),
        (True, True, 1, True, None),
        (False, True, 1, False, (True, False)),
    ],
)
async def test_dependency_feature_flag_matrix(
    monkeypatch,
    shadow: bool,
    enforcement: bool,
    expected_service_calls: int,
    expected_block: bool,
    expected_context: tuple[bool, bool] | None,
):
    from app.core.config import settings
    from app.platform_billing.domain.capability_decision import CapabilityDecision
    from app.platform_billing.services import capability_authorization_service as auth_service

    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", shadow)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", enforcement)
    calls = 0

    async def deny(self, **kwargs):
        nonlocal calls
        calls += 1
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=False,
                decision_code="PLATFORM_ACCESS_DENIED",
                safe_reason_code="access_mode_denied",
                capability_key="branches.create",
                operation_class=OperationClass.capacity_increase.value,
                access_mode="read_only",
                required_feature_key="limits.branches.active",
                entitlement_value=3,
                usage_value=3,
                limit_value=3,
                projection_freshness="fresh",
                fallback_used=False,
                recompute_attempted=False,
                source_subscription_version=1,
                decision_timestamp=datetime.now(timezone.utc),
            )
        )

    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)
    staff = Staff(id=uuid4(), org_id=uuid4(), gym_id=None, role="owner")
    dependency = require_platform_capability(
        "branches.create",
        OperationClass.capacity_increase.value,
    )

    request = SimpleNamespace(
        state=SimpleNamespace(correlation_id="test"),
        url=SimpleNamespace(path="/gyms"),
    )
    if expected_block:
        with pytest.raises(HTTPException):
            await dependency(request=request, staff=staff, db=object())
    else:
        context = await dependency(request=request, staff=staff, db=object())
        assert (context.shadow_enabled, context.enforcement_enabled) == expected_context
    assert calls == expected_service_calls


@pytest.mark.asyncio
async def test_dependency_enforcement_blocks_denied_decision(monkeypatch):
    from app.core.config import settings
    from app.platform_billing.domain.capability_decision import CapabilityDecision
    from app.platform_billing.services import capability_authorization_service as auth_service

    monkeypatch.setattr(settings, "PLATFORM_BILLING_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLATFORM_BILLING_SHADOW_RESOLVER", True)

    async def deny(self, **kwargs):
        return auth_service.AuthorizationServiceResult(
            decision=CapabilityDecision(
                allowed=False,
                decision_code="PLATFORM_ACCESS_DENIED",
                safe_reason_code="access_mode_denied",
                capability_key="branches.create",
                operation_class=OperationClass.capacity_increase.value,
                access_mode="read_only",
                required_feature_key="limits.branches.active",
                entitlement_value=3,
                usage_value=3,
                limit_value=3,
                projection_freshness="fresh",
                fallback_used=False,
                recompute_attempted=False,
                source_subscription_version=1,
                decision_timestamp=datetime.now(timezone.utc),
            )
        )

    monkeypatch.setattr(auth_service.CapabilityAuthorizationService, "authorize", deny)
    staff = Staff(id=uuid4(), org_id=uuid4(), gym_id=None, role="owner")
    dependency = require_platform_capability(
        "branches.create",
        OperationClass.capacity_increase.value,
    )

    with pytest.raises(HTTPException) as exc:
        await dependency(
            request=SimpleNamespace(state=SimpleNamespace(correlation_id="test"), url=SimpleNamespace(path="/gyms")),
            staff=staff,
            db=object(),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "PLATFORM_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_read_api_disabled_returns_stable_error(monkeypatch):
    from app.core.config import settings
    from app.platform_billing.api.tenant import get_platform_billing_summary

    monkeypatch.setattr(settings, "PLATFORM_BILLING_READ_API", False)
    staff = Staff(id=uuid4(), org_id=uuid4(), gym_id=None, role="owner")

    with pytest.raises(HTTPException) as exc:
        await get_platform_billing_summary(
            context=SimpleNamespace(staff=staff),
            db=object(),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "PLATFORM_BILLING_READ_API_DISABLED"


@pytest.mark.asyncio
async def test_authorization_service_capacity_and_projection_states(db_session):
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()
    now = datetime.now(timezone.utc)
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_access_projection (
            organization_id, subscription_id, mode, reason_code, reason_detail_safe,
            effective_from, recovery_actions_json, source_subscription_version,
            resolution_version, input_sha256
        )
        VALUES (
            :org1, :subscription, 'full', 'FULL_ACCESS', '',
            :now, '["VIEW_PLAN_BILLING"]'::jsonb, 1, 1, :sha
        );

        INSERT INTO platform_entitlement_projection (
            organization_id, feature_key, value_type, value_integer, effective_from,
            source_subscription_version, resolution_version, input_sha256
        )
        VALUES (
            :org1, 'limits.branches.active', 'integer', 3, :now, 1, 1, :sha
        );
        """,
        {"org1": ORG_1, "subscription": ids["subscription"], "now": now, "sha": "c" * 64},
    )

    async def set_usage(value: int) -> None:
        await exec_sql(
            """
            SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

            DELETE FROM platform_usage_projection
            WHERE organization_id = :org1 AND metric_key = 'limits.branches.active';
            INSERT INTO platform_usage_projection (
                organization_id, metric_key, current_value, measured_at, stale_after
            )
            VALUES (:org1, 'limits.branches.active', :value, :now, :stale_after);
            """,
            {
                "org1": ORG_1,
                "value": value,
                "now": now,
                "stale_after": now + timedelta(minutes=5),
            },
        )

    for value, expected_allowed, expected_code in [
        (2, True, "ALLOWED"),
        (3, False, "PLATFORM_USAGE_LIMIT_REACHED"),
        (4, False, "PLATFORM_USAGE_LIMIT_REACHED"),
    ]:
        await set_usage(value)
        await db_session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
            {"org1": ORG_1},
        )
        result = await CapabilityAuthorizationService(db_session).authorize(
            organization_id=UUID(ORG_1),
            capability_key="branches.create",
            operation_class=OperationClass.capacity_increase.value,
        )
        assert result.decision.allowed is expected_allowed
        assert result.decision.decision_code == expected_code
        assert result.decision.usage_value == value

    await set_usage(4)
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
        {"org1": ORG_1},
    )
    decrease = await CapabilityAuthorizationService(db_session).authorize(
        organization_id=UUID(ORG_1),
        capability_key="branches.change_status",
        operation_class=OperationClass.capacity_decrease.value,
    )
    assert decrease.decision.allowed is True

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);
        DELETE FROM platform_usage_projection WHERE organization_id = :org1
        """,
        {"org1": ORG_1},
    )
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
        {"org1": ORG_1},
    )
    unavailable = await CapabilityAuthorizationService(db_session).authorize(
        organization_id=UUID(ORG_1),
        capability_key="branches.create",
        operation_class=OperationClass.capacity_increase.value,
    )
    assert unavailable.decision.allowed is False
    assert unavailable.decision.decision_code == "ACCESS_DECISION_UNAVAILABLE"
    assert unavailable.decision.usage_value is None


@pytest.mark.asyncio
async def test_authorization_service_recompute_timeout_and_safe_audit(db_session):
    await cleanup_phase1_tables()
    await seed_organizations()
    ids = await seed_billing_account_and_subscription()
    now = datetime.now(timezone.utc)
    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        INSERT INTO platform_access_projection (
            organization_id, subscription_id, mode, reason_code, reason_detail_safe,
            effective_from, recovery_actions_json, source_subscription_version,
            resolution_version, input_sha256
        )
        VALUES (
            :org1, :subscription, 'full', 'FULL_ACCESS', '',
            :now, '["VIEW_PLAN_BILLING"]'::jsonb, 2, 1, :sha
        );
        """,
        {"org1": ORG_1, "subscription": ids["subscription"], "now": now, "sha": "d" * 64},
    )

    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
        {"org1": ORG_1},
    )
    result = await CapabilityAuthorizationService(db_session).authorize(
        organization_id=UUID(ORG_1),
        capability_key="branches.view",
        operation_class=OperationClass.safe_read.value,
        correlation_id="safe-correlation",
    )
    assert result.decision.allowed is False
    assert result.decision.decision_code == "PLATFORM_PROJECTION_INVALID"
    await db_session.flush()

    audit = await db_session.execute(
        text(
            """
            SELECT metadata_redacted_json
            FROM platform_billing_audit_events
            WHERE organization_id = :org1
              AND action = 'capability.projection_invalid'
            """
        ),
        {"org1": ORG_1},
    )
    payload = audit.scalar_one()
    assert payload == {
        "capability_key": "branches.view",
        "operation_class": OperationClass.safe_read.value,
        "correlation_id": "safe-correlation",
    }
    assert ORG_1 not in str(payload)

    async def slow_recompute(*args, **kwargs):
        await asyncio.sleep(1)
        return True

    await exec_sql(
        """
        SELECT pg_catalog.set_config('app.current_org_id', :org1, true);

        DELETE FROM platform_access_projection
        WHERE organization_id = :org1;

        INSERT INTO platform_access_projection (
            organization_id, subscription_id, mode, reason_code, reason_detail_safe,
            effective_from, recovery_actions_json, source_subscription_version,
            resolution_version, input_sha256
        )
        VALUES (
            :org1, :subscription, 'full', 'FULL_ACCESS', '',
            :now, '["VIEW_PLAN_BILLING"]'::jsonb, 0, 1, :sha
        )
        """,
        {"org1": ORG_1, "subscription": ids["subscription"], "now": now, "sha": "e" * 64},
    )
    await db_session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :org1, true)"),
        {"org1": ORG_1},
    )
    timeout_result = await CapabilityAuthorizationService(
        db_session,
        recompute_callback=slow_recompute,
    ).authorize(
        organization_id=UUID(ORG_1),
        capability_key="branches.view",
        operation_class=OperationClass.safe_read.value,
    )
    assert timeout_result.decision.recompute_attempted is True
    assert timeout_result.recompute_error == "TimeoutError"


def test_phase3_feature_flag_defaults_are_false():
    from app.core.config import settings

    flags = {
        "PLATFORM_BILLING_READ_API": settings.PLATFORM_BILLING_READ_API,
        "PLATFORM_BILLING_SHADOW_RESOLVER": settings.PLATFORM_BILLING_SHADOW_RESOLVER,
        "PLATFORM_BILLING_ENFORCEMENT": settings.PLATFORM_BILLING_ENFORCEMENT,
        "PLATFORM_BILLING_FRONTEND_SHELL": settings.PLATFORM_BILLING_FRONTEND_SHELL,
        "PLATFORM_BILLING_CHECKOUT": settings.PLATFORM_BILLING_CHECKOUT,
        "PLATFORM_BILLING_WEBHOOK_PROCESSING": settings.PLATFORM_BILLING_WEBHOOK_PROCESSING,
        "PLATFORM_BILLING_DUNNING_TRANSITIONS": settings.PLATFORM_BILLING_DUNNING_TRANSITIONS,
        "PLATFORM_BILLING_NOTIFICATIONS": settings.PLATFORM_BILLING_NOTIFICATIONS,
    }
    assert flags == {key: False for key in flags}


@pytest.mark.asyncio
async def test_read_api_uses_authenticated_tenant_and_sanitized_payload(monkeypatch):
    from app.core.config import settings
    from app.platform_billing.api.tenant import get_platform_billing_summary

    monkeypatch.setattr(settings, "PLATFORM_BILLING_READ_API", True)
    requested_orgs: list[object] = []

    async def fake_summary(self, organization_id):
        from app.platform_billing.api.schemas import (
            PlatformBillingAccessSummary,
            PlatformBillingDecisionAvailability,
            PlatformBillingPeriodSummary,
            PlatformBillingPlanSummary,
            PlatformBillingSummaryResponse,
        )

        requested_orgs.append(organization_id)
        return PlatformBillingSummaryResponse(
            organization_id=str(organization_id),
            access=PlatformBillingAccessSummary(
                mode="read_only",
                safe_reason_code="DECISION_UNAVAILABLE",
                recovery_actions=["VIEW_PLAN_BILLING", "CONTACT_SUPPORT"],
                projection_freshness="missing",
            ),
            plan=PlatformBillingPlanSummary(),
            billing_period=PlatformBillingPeriodSummary(),
            entitlements=[],
            usage=[],
            decision_availability=PlatformBillingDecisionAvailability(
                available=False,
                reason="projection_missing",
            ),
            server_time=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "app.platform_billing.services.billing_summary_service.PlatformBillingSummaryService.get_summary",
        fake_summary,
    )
    staff = Staff(id=uuid4(), org_id=uuid4(), gym_id=None, role="owner")
    response = await get_platform_billing_summary(
        context=SimpleNamespace(staff=staff),
        db=object(),
    )
    payload = response.model_dump()

    assert requested_orgs == [staff.org_id]
    assert payload["organization_id"] == str(staff.org_id)
    assert "policy" not in str(payload).lower()
    assert "metadata_redacted_json" not in str(payload)
    assert "provider" not in str(payload).lower()
    assert "unpaid" not in str(payload).lower()
    assert "cancelled" not in str(payload).lower()
    assert set(payload["access"]["recovery_actions"]) <= {"VIEW_PLAN_BILLING", "CONTACT_SUPPORT"}
