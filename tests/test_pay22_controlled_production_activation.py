from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.payment_activation.domain import (
    MAX_STAGE3_MERCHANTS,
    MAX_STAGE4_BASIS_POINTS,
    ActivationAuthorization,
    ActivationCapability,
    ActivationRuntime,
    ActivationStage,
    KillSwitches,
    ProductionActivationDenied,
    ProductionActivationPolicy,
    kill_switch_names,
)
from app.payment_activation.service import PaymentActivationService


CERTIFIED_SHA = "a" * 40
INTERNAL = uuid.UUID("22000000-0000-4000-8000-000000000001")
SELECTED = uuid.UUID("22000000-0000-4000-8000-000000000002")
MERCHANT = uuid.UUID("22000000-0000-4000-8000-000000000003")
OUTSIDER = uuid.UUID("22000000-0000-4000-8000-000000000004")


def authorization() -> ActivationAuthorization:
    return ActivationAuthorization(
        authorization_id="PAY22-AUTH-TEST",
        authorized_sha=CERTIFIED_SHA,
        authorized_by="human-release-approver",
        authorized_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )


def switches(**overrides: bool) -> KillSwitches:
    values = {name: True for name in kill_switch_names()}
    values.update(overrides)
    return KillSwitches(**values)


def runtime(stage: ActivationStage, **overrides) -> ActivationRuntime:
    values = dict(
        certified_sha=CERTIFIED_SHA,
        deployed_sha=CERTIFIED_SHA,
        stage=stage,
        live_provider_configured=True,
        provider_egress_enabled=True,
        kill_switches=switches(),
        authorization=authorization(),
        internal_organization_id=INTERNAL,
        selected_test_organization_id=SELECTED,
        merchant_cohort=(MERCHANT,),
        percentage_basis_points=0,
        rollout_seed="pay22-stable-rollout-v1",
    )
    if stage < ActivationStage.SMALL_MERCHANT_COHORT:
        values["merchant_cohort"] = ()
    if stage == ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED:
        values["provider_egress_enabled"] = False
        values["kill_switches"] = KillSwitches()
        values["internal_organization_id"] = None
        values["selected_test_organization_id"] = None
    if stage == ActivationStage.INTERNAL_ORGANIZATION:
        values["selected_test_organization_id"] = None
    if stage == ActivationStage.LIMITED_PERCENTAGE:
        values["percentage_basis_points"] = 500
    values.update(overrides)
    return ActivationRuntime(**values)


def test_exact_sha_and_human_authorization_are_mandatory():
    no_auth = runtime(
        ActivationStage.INTERNAL_ORGANIZATION,
        authorization=None,
    )
    decision = ProductionActivationPolicy(no_auth).decide(
        ActivationCapability.CHECKOUT,
        organization_id=INTERNAL,
    )
    assert decision.allowed is False
    assert decision.code == "activation.human_authorization.required"

    wrong_sha = runtime(
        ActivationStage.INTERNAL_ORGANIZATION,
        deployed_sha="b" * 40,
    )
    decision = ProductionActivationPolicy(wrong_sha).decide(
        ActivationCapability.CHECKOUT,
        organization_id=INTERNAL,
    )
    assert decision.allowed is False
    assert decision.code == "activation.exact_sha.mismatch"


def test_stage0_requires_live_config_but_blocks_egress_and_every_capability():
    stage0 = runtime(ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED)
    assert stage0.validate() == ()

    for capability in ActivationCapability:
        decision = ProductionActivationPolicy(stage0).decide(
            capability,
            organization_id=INTERNAL,
        )
        assert decision.allowed is False
        assert decision.code.startswith("activation.kill_switch.")

    assert "activation.stage0.egress_must_be_blocked" in runtime(
        ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED,
        provider_egress_enabled=True,
    ).validate()
    assert "activation.stage0.capabilities_must_be_disabled" in runtime(
        ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED,
        kill_switches=switches(),
    ).validate()


def test_stage1_is_internal_organization_only():
    policy = ProductionActivationPolicy(runtime(ActivationStage.INTERNAL_ORGANIZATION))
    assert policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=INTERNAL,
    ).allowed
    assert not policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=SELECTED,
    ).allowed


def test_stage2_adds_exactly_selected_test_organization():
    policy = ProductionActivationPolicy(
        runtime(ActivationStage.SELECTED_TEST_ORGANIZATION)
    )
    assert policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=INTERNAL,
    ).cohort_source == "internal_organization"
    assert policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=SELECTED,
    ).cohort_source == "selected_test_organization"
    assert not policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=MERCHANT,
    ).allowed


def test_stage3_is_bounded_explicit_merchant_cohort():
    policy = ProductionActivationPolicy(runtime(ActivationStage.SMALL_MERCHANT_COHORT))
    assert policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=MERCHANT,
    ).cohort_source == "merchant_cohort"
    assert not policy.decide(
        ActivationCapability.CHECKOUT,
        organization_id=OUTSIDER,
    ).allowed

    too_many = tuple(uuid.uuid4() for _ in range(MAX_STAGE3_MERCHANTS + 1))
    assert "activation.stage3.cohort_too_large" in runtime(
        ActivationStage.SMALL_MERCHANT_COHORT,
        merchant_cohort=too_many,
    ).validate()


def test_stage4_percentage_is_deterministic_and_capped():
    stage4 = runtime(ActivationStage.LIMITED_PERCENTAGE)
    policy = ProductionActivationPolicy(stage4)
    first = policy.decide(
        ActivationCapability.WEBHOOKS,
        organization_id=OUTSIDER,
    )
    second = policy.decide(
        ActivationCapability.WEBHOOKS,
        organization_id=OUTSIDER,
    )
    assert first == second

    assert "activation.stage4.percentage_out_of_range" in runtime(
        ActivationStage.LIMITED_PERCENTAGE,
        percentage_basis_points=MAX_STAGE4_BASIS_POINTS + 1,
    ).validate()


def test_stage5_is_general_availability_but_still_respects_kill_switches():
    stage5 = runtime(ActivationStage.GENERAL_AVAILABILITY)
    policy = ProductionActivationPolicy(stage5)
    assert policy.decide(
        ActivationCapability.WEBHOOKS,
        organization_id=OUTSIDER,
    ).cohort_source == "general_availability"

    disabled = replace(stage5, kill_switches=switches(webhooks=False))
    decision = ProductionActivationPolicy(disabled).decide(
        ActivationCapability.WEBHOOKS,
        organization_id=OUTSIDER,
    )
    assert not decision.allowed
    assert decision.code == "activation.kill_switch.webhooks.disabled"


def test_provider_egress_outage_does_not_disable_inbound_or_internal_capabilities():
    egress_blocked = runtime(
        ActivationStage.GENERAL_AVAILABILITY,
        provider_egress_enabled=False,
    )
    policy = ProductionActivationPolicy(egress_blocked)

    for capability in (
        ActivationCapability.CHECKOUT,
        ActivationCapability.REFUND_EXECUTION,
        ActivationCapability.RECURRING_BILLING,
    ):
        decision = policy.decide(capability, organization_id=OUTSIDER)
        assert not decision.allowed
        assert decision.code == "activation.provider_egress.blocked"

    for capability in (
        ActivationCapability.WEBHOOKS,
        ActivationCapability.PAYMENT_APPLICATION,
        ActivationCapability.SUBSCRIPTION_ACTIVATION,
        ActivationCapability.DUNNING,
        ActivationCapability.PLATFORM_BILLING,
    ):
        assert policy.decide(capability, organization_id=OUTSIDER).allowed


def test_each_requested_kill_switch_is_independent():
    base = runtime(ActivationStage.GENERAL_AVAILABILITY)
    expected = {capability.value for capability in ActivationCapability}
    assert set(kill_switch_names()) == expected

    for disabled_capability in ActivationCapability:
        posture = replace(
            base,
            kill_switches=replace(
                switches(),
                **{disabled_capability.value: False},
            ),
        )
        policy = ProductionActivationPolicy(posture)
        for capability in ActivationCapability:
            decision = policy.decide(capability, organization_id=OUTSIDER)
            if capability == disabled_capability:
                assert not decision.allowed
                assert decision.code == (
                    f"activation.kill_switch.{capability.value}.disabled"
                )
            else:
                assert decision.allowed


def test_kill_switch_update_does_not_require_platform_restart():
    service = PaymentActivationService(runtime(ActivationStage.GENERAL_AVAILABILITY))
    assert service.decision(
        ActivationCapability.CHECKOUT,
        organization_id=OUTSIDER,
    ).allowed

    disabled_checkout = service.with_kill_switches(
        replace(switches(), checkout=False)
    )
    assert not disabled_checkout.decision(
        ActivationCapability.CHECKOUT,
        organization_id=OUTSIDER,
    ).allowed
    assert disabled_checkout.decision(
        ActivationCapability.WEBHOOKS,
        organization_id=OUTSIDER,
    ).allowed


def test_require_raises_sanitized_decision_without_secret_material():
    policy = ProductionActivationPolicy(
        runtime(
            ActivationStage.GENERAL_AVAILABILITY,
            kill_switches=switches(refund_execution=False),
        )
    )
    with pytest.raises(ProductionActivationDenied) as exc:
        policy.require(
            ActivationCapability.REFUND_EXECUTION,
            organization_id=OUTSIDER,
        )

    assert exc.value.decision.code == "activation.kill_switch.refund_execution.disabled"
    rendered = str(exc.value).lower()
    for forbidden in ("secret", "password", "token", "rzp_live_"):
        assert forbidden not in rendered


def test_no_global_payment_switch_exists():
    names = set(ActivationRuntime.__dataclass_fields__) | set(
        KillSwitches.__dataclass_fields__
    )
    assert "payments_enabled" not in names
    assert "global_payment_switch" not in names
