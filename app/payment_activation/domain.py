from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Iterable


class ActivationStage(IntEnum):
    LIVE_CONFIG_EGRESS_BLOCKED = 0
    INTERNAL_ORGANIZATION = 1
    SELECTED_TEST_ORGANIZATION = 2
    SMALL_MERCHANT_COHORT = 3
    LIMITED_PERCENTAGE = 4
    GENERAL_AVAILABILITY = 5


class ActivationCapability(StrEnum):
    CHECKOUT = "checkout"
    WEBHOOKS = "webhooks"
    PAYMENT_APPLICATION = "payment_application"
    SUBSCRIPTION_ACTIVATION = "subscription_activation"
    REFUND_EXECUTION = "refund_execution"
    RECURRING_BILLING = "recurring_billing"
    DUNNING = "dunning"
    PLATFORM_BILLING = "platform_billing"


OUTBOUND_PROVIDER_CAPABILITIES = frozenset(
    {
        ActivationCapability.CHECKOUT,
        ActivationCapability.REFUND_EXECUTION,
        ActivationCapability.RECURRING_BILLING,
    }
)

MAX_STAGE3_MERCHANTS = 10
MAX_STAGE4_BASIS_POINTS = 1_000


@dataclass(frozen=True)
class KillSwitches:
    checkout: bool = False
    webhooks: bool = False
    payment_application: bool = False
    subscription_activation: bool = False
    refund_execution: bool = False
    recurring_billing: bool = False
    dunning: bool = False
    platform_billing: bool = False

    def enabled(self, capability: ActivationCapability) -> bool:
        return getattr(self, capability.value) is True

    def enabled_capabilities(self) -> tuple[ActivationCapability, ...]:
        return tuple(
            capability
            for capability in ActivationCapability
            if self.enabled(capability)
        )

    def invalid_value_names(self) -> tuple[str, ...]:
        """Return switches whose values are not actual booleans.

        ``bool`` is intentionally checked by exact type.  Values such as ``1``
        must not silently become activation authority merely because Python
        considers them truthy.
        """

        return tuple(
            field.name
            for field in fields(self)
            if type(getattr(self, field.name)) is not bool
        )


@dataclass(frozen=True)
class ActivationAuthorization:
    authorization_id: str
    authorized_sha: str
    authorized_stage: ActivationStage
    authorized_by: str
    authorized_at: datetime

    def is_complete(self) -> bool:
        return bool(
            type(self.authorization_id) is str
            and self.authorization_id.strip()
            and _is_sha(self.authorized_sha)
            and type(self.authorized_stage) is ActivationStage
            and type(self.authorized_by) is str
            and self.authorized_by.strip()
            and _is_aware_datetime(self.authorized_at)
        )


@dataclass(frozen=True)
class ActivationRuntime:
    certified_sha: str
    deployed_sha: str
    stage: ActivationStage
    live_provider_configured: bool
    provider_egress_enabled: bool
    kill_switches: KillSwitches = KillSwitches()
    authorization: ActivationAuthorization | None = None
    internal_organization_id: uuid.UUID | None = None
    selected_test_organization_id: uuid.UUID | None = None
    merchant_cohort: tuple[uuid.UUID, ...] = ()
    percentage_basis_points: int = 0
    rollout_seed: str = ""

    def validate(self) -> tuple[str, ...]:
        failures: list[str] = []

        stage = self.stage if type(self.stage) is ActivationStage else None
        if stage is None:
            failures.append("activation.stage.invalid")

        if not _is_sha(self.certified_sha):
            failures.append("activation.certified_sha.invalid")
        if not _is_sha(self.deployed_sha):
            failures.append("activation.deployed_sha.invalid")
        if self.deployed_sha != self.certified_sha:
            failures.append("activation.exact_sha.mismatch")

        if type(self.live_provider_configured) is not bool:
            failures.append("activation.live_provider.configuration_invalid")
        elif not self.live_provider_configured:
            failures.append("activation.live_provider.not_configured")

        if type(self.provider_egress_enabled) is not bool:
            failures.append("activation.provider_egress.configuration_invalid")

        switches_valid = type(self.kill_switches) is KillSwitches
        switches = self.kill_switches if switches_valid else KillSwitches()
        if not switches_valid:
            failures.append("activation.kill_switches.invalid")
        elif switches.invalid_value_names():
            failures.append("activation.kill_switches.values_invalid")

        cohort_valid = type(self.merchant_cohort) is tuple and all(
            _is_non_nil_uuid(organization_id)
            for organization_id in self.merchant_cohort
        )
        cohort = self.merchant_cohort if cohort_valid else ()
        if not cohort_valid:
            failures.append("activation.merchant_cohort.invalid")

        percentage_valid = type(self.percentage_basis_points) is int
        if not percentage_valid:
            failures.append("activation.percentage.invalid")
        if type(self.rollout_seed) is not str:
            failures.append("activation.rollout_seed.invalid")

        if (
            self.internal_organization_id is not None
            and not _is_non_nil_uuid(self.internal_organization_id)
        ):
            failures.append("activation.internal_organization.invalid")
        if (
            self.selected_test_organization_id is not None
            and not _is_non_nil_uuid(self.selected_test_organization_id)
        ):
            failures.append("activation.selected_test_organization.invalid")

        auth = self.authorization
        if type(auth) is not ActivationAuthorization or not auth.is_complete():
            failures.append("activation.human_authorization.required")
        elif auth.authorized_sha != self.certified_sha:
            failures.append("activation.human_authorization.sha_mismatch")
        elif auth.authorized_at > datetime.now(timezone.utc):
            failures.append("activation.human_authorization.future_timestamp")
        elif stage is not None and auth.authorized_stage != stage:
            failures.append("activation.human_authorization.stage_mismatch")

        if stage == ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED:
            if self.provider_egress_enabled is True:
                failures.append("activation.stage0.egress_must_be_blocked")
            if switches.enabled_capabilities():
                failures.append("activation.stage0.capabilities_must_be_disabled")
            if self.percentage_basis_points != 0:
                failures.append("activation.stage0.percentage_must_be_zero")
            if cohort:
                failures.append("activation.stage0.cohort_must_be_empty")
            if (
                self.internal_organization_id is not None
                or self.selected_test_organization_id is not None
            ):
                failures.append("activation.stage0.organizations_must_be_empty")

        if stage is not None and stage >= ActivationStage.INTERNAL_ORGANIZATION:
            if not _is_non_nil_uuid(self.internal_organization_id):
                failures.append("activation.internal_organization.required")

        if stage is not None and stage >= ActivationStage.SELECTED_TEST_ORGANIZATION:
            if not _is_non_nil_uuid(self.selected_test_organization_id):
                failures.append("activation.selected_test_organization.required")

        if stage == ActivationStage.SMALL_MERCHANT_COHORT:
            if not cohort:
                failures.append("activation.stage3.cohort_required")
            if len(set(cohort)) != len(cohort):
                failures.append("activation.stage3.cohort_duplicates")
            if len(cohort) > MAX_STAGE3_MERCHANTS:
                failures.append("activation.stage3.cohort_too_large")
            if self.percentage_basis_points != 0:
                failures.append("activation.stage3.percentage_must_be_zero")

        if stage == ActivationStage.LIMITED_PERCENTAGE:
            if not percentage_valid or not (
                1 <= self.percentage_basis_points <= MAX_STAGE4_BASIS_POINTS
            ):
                failures.append("activation.stage4.percentage_out_of_range")
            if (
                type(self.rollout_seed) is not str
                or not self.rollout_seed.strip()
            ):
                failures.append("activation.stage4.rollout_seed_required")

        if stage == ActivationStage.GENERAL_AVAILABILITY:
            if (
                not percentage_valid
                or self.percentage_basis_points not in {0, 10_000}
            ):
                failures.append("activation.stage5.percentage_invalid")

        if (
            stage is not None
            and stage < ActivationStage.SMALL_MERCHANT_COHORT
            and cohort
        ):
            failures.append("activation.cohort.not_allowed_before_stage3")
        if (
            stage is not None
            and stage < ActivationStage.LIMITED_PERCENTAGE
            and self.percentage_basis_points
        ):
            failures.append("activation.percentage.not_allowed_before_stage4")

        return tuple(dict.fromkeys(failures))


@dataclass(frozen=True)
class ActivationDecision:
    allowed: bool
    code: str
    capability: ActivationCapability
    stage: ActivationStage
    organization_id: uuid.UUID | None
    cohort_source: str | None
    authorization_id: str | None
    certified_sha: str


@dataclass(frozen=True)
class StageTransitionDecision:
    allowed: bool
    code: str
    from_stage: ActivationStage
    to_stage: ActivationStage


def validate_stage_transition(
    current: ActivationRuntime,
    proposed: ActivationRuntime,
) -> StageTransitionDecision:
    proposed_failures = proposed.validate()
    if proposed_failures:
        return StageTransitionDecision(
            allowed=False,
            code=proposed_failures[0],
            from_stage=current.stage,
            to_stage=proposed.stage,
        )

    if type(current.stage) is not ActivationStage:
        return StageTransitionDecision(
            allowed=False,
            code="activation.stage.invalid",
            from_stage=current.stage,
            to_stage=proposed.stage,
        )

    if proposed.stage > current.stage + 1:
        return StageTransitionDecision(
            allowed=False,
            code="activation.stage_transition.skip_forbidden",
            from_stage=current.stage,
            to_stage=proposed.stage,
        )

    return StageTransitionDecision(
        allowed=True,
        code="activation.stage_transition.allowed",
        from_stage=current.stage,
        to_stage=proposed.stage,
    )


class ProductionActivationPolicy:
    """Legacy in-process PAY-22 policy evaluator.

    This class remains for PAY-22 compatibility and pure policy tests.  It is
    not a durable activation authority and must not gate provider I/O in a
    multi-process deployment.  PAY-24A authority is exposed only through the
    bounded PostgreSQL façade in :mod:`app.payment_activation.authority`.
    """

    def __init__(self, runtime: ActivationRuntime):
        self.runtime = runtime

    def decide(
        self,
        capability: ActivationCapability,
        *,
        organization_id: uuid.UUID | None,
    ) -> ActivationDecision:
        failures = self.runtime.validate()
        if failures:
            return self._deny(capability, organization_id, failures[0])

        if not self.runtime.kill_switches.enabled(capability):
            return self._deny(
                capability,
                organization_id,
                f"activation.kill_switch.{capability.value}.disabled",
            )

        if (
            capability in OUTBOUND_PROVIDER_CAPABILITIES
            and not self.runtime.provider_egress_enabled
        ):
            return self._deny(
                capability,
                organization_id,
                "activation.provider_egress.blocked",
            )

        cohort_source = self._cohort_source(organization_id)
        if cohort_source is None:
            return self._deny(
                capability,
                organization_id,
                "activation.organization.not_in_rollout",
            )

        auth = self.runtime.authorization
        return ActivationDecision(
            allowed=True,
            code="activation.allowed",
            capability=capability,
            stage=self.runtime.stage,
            organization_id=organization_id,
            cohort_source=cohort_source,
            authorization_id=auth.authorization_id if auth else None,
            certified_sha=self.runtime.certified_sha,
        )

    def require(
        self,
        capability: ActivationCapability,
        *,
        organization_id: uuid.UUID | None,
    ) -> ActivationDecision:
        decision = self.decide(capability, organization_id=organization_id)
        if not decision.allowed:
            raise ProductionActivationDenied(decision)
        return decision

    def _cohort_source(self, organization_id: uuid.UUID | None) -> str | None:
        if not _is_non_nil_uuid(organization_id):
            return None

        stage = self.runtime.stage
        if stage == ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED:
            return None

        if organization_id == self.runtime.internal_organization_id:
            return "internal_organization"

        if (
            stage >= ActivationStage.SELECTED_TEST_ORGANIZATION
            and organization_id == self.runtime.selected_test_organization_id
        ):
            return "selected_test_organization"

        if stage >= ActivationStage.SMALL_MERCHANT_COHORT:
            if organization_id in self.runtime.merchant_cohort:
                return "merchant_cohort"

        if stage == ActivationStage.LIMITED_PERCENTAGE:
            if _bucket(
                organization_id,
                self.runtime.rollout_seed,
            ) < self.runtime.percentage_basis_points:
                return "limited_percentage"

        if stage == ActivationStage.GENERAL_AVAILABILITY:
            return "general_availability"

        return None

    def _deny(
        self,
        capability: ActivationCapability,
        organization_id: uuid.UUID | None,
        code: str,
    ) -> ActivationDecision:
        auth = self.runtime.authorization
        return ActivationDecision(
            allowed=False,
            code=code,
            capability=capability,
            stage=self.runtime.stage,
            organization_id=organization_id,
            cohort_source=None,
            authorization_id=(
                auth.authorization_id
                if type(auth) is ActivationAuthorization
                and type(auth.authorization_id) is str
                else None
            ),
            certified_sha=self.runtime.certified_sha,
        )


class ProductionActivationDenied(RuntimeError):
    def __init__(self, decision: ActivationDecision):
        self.decision = decision
        super().__init__(decision.code)


def _bucket(organization_id: uuid.UUID, rollout_seed: str) -> int:
    digest = hashlib.sha256(
        f"{rollout_seed}:{organization_id}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _is_sha(value: object) -> bool:
    return bool(
        type(value) is str
        and len(value) == 40
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _is_aware_datetime(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _is_non_nil_uuid(value: object) -> bool:
    return type(value) is uuid.UUID and value.int != 0


def kill_switch_names() -> tuple[str, ...]:
    return tuple(field.name for field in fields(KillSwitches))


def capabilities(values: Iterable[str]) -> tuple[ActivationCapability, ...]:
    return tuple(ActivationCapability(value) for value in values)
