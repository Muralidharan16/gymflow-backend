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
        return bool(getattr(self, capability.value))

    def enabled_capabilities(self) -> tuple[ActivationCapability, ...]:
        return tuple(
            capability
            for capability in ActivationCapability
            if self.enabled(capability)
        )


@dataclass(frozen=True)
class ActivationAuthorization:
    authorization_id: str
    authorized_sha: str
    authorized_by: str
    authorized_at: datetime

    def is_complete(self) -> bool:
        return bool(
            self.authorization_id.strip()
            and self.authorized_sha.strip()
            and self.authorized_by.strip()
            and self.authorized_at.tzinfo is not None
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

        if not _is_sha(self.certified_sha):
            failures.append("activation.certified_sha.invalid")
        if not _is_sha(self.deployed_sha):
            failures.append("activation.deployed_sha.invalid")
        if self.deployed_sha != self.certified_sha:
            failures.append("activation.exact_sha.mismatch")

        if not self.live_provider_configured:
            failures.append("activation.live_provider.not_configured")

        auth = self.authorization
        if auth is None or not auth.is_complete():
            failures.append("activation.human_authorization.required")
        elif auth.authorized_sha != self.certified_sha:
            failures.append("activation.human_authorization.sha_mismatch")
        elif auth.authorized_at > datetime.now(timezone.utc):
            failures.append("activation.human_authorization.future_timestamp")

        if self.stage == ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED:
            if self.provider_egress_enabled:
                failures.append("activation.stage0.egress_must_be_blocked")
            if self.kill_switches.enabled_capabilities():
                failures.append("activation.stage0.capabilities_must_be_disabled")
            if self.percentage_basis_points != 0:
                failures.append("activation.stage0.percentage_must_be_zero")
            if self.merchant_cohort:
                failures.append("activation.stage0.cohort_must_be_empty")

        if self.stage >= ActivationStage.INTERNAL_ORGANIZATION:
            if self.internal_organization_id is None:
                failures.append("activation.internal_organization.required")

        if self.stage >= ActivationStage.SELECTED_TEST_ORGANIZATION:
            if self.selected_test_organization_id is None:
                failures.append("activation.selected_test_organization.required")

        if self.stage == ActivationStage.SMALL_MERCHANT_COHORT:
            if not self.merchant_cohort:
                failures.append("activation.stage3.cohort_required")
            if len(set(self.merchant_cohort)) != len(self.merchant_cohort):
                failures.append("activation.stage3.cohort_duplicates")
            if len(self.merchant_cohort) > MAX_STAGE3_MERCHANTS:
                failures.append("activation.stage3.cohort_too_large")
            if self.percentage_basis_points != 0:
                failures.append("activation.stage3.percentage_must_be_zero")

        if self.stage == ActivationStage.LIMITED_PERCENTAGE:
            if not (1 <= self.percentage_basis_points <= MAX_STAGE4_BASIS_POINTS):
                failures.append("activation.stage4.percentage_out_of_range")
            if not self.rollout_seed.strip():
                failures.append("activation.stage4.rollout_seed_required")

        if self.stage == ActivationStage.GENERAL_AVAILABILITY:
            if self.percentage_basis_points not in {0, 10_000}:
                failures.append("activation.stage5.percentage_invalid")

        if self.stage < ActivationStage.SMALL_MERCHANT_COHORT and self.merchant_cohort:
            failures.append("activation.cohort.not_allowed_before_stage3")
        if self.stage < ActivationStage.LIMITED_PERCENTAGE and self.percentage_basis_points:
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


class ProductionActivationPolicy:
    """Fail-closed production activation authority."""

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
        if organization_id is None:
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
            authorization_id=auth.authorization_id if auth else None,
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


def _is_sha(value: str) -> bool:
    stripped = value.strip().lower()
    return len(stripped) == 40 and all(ch in "0123456789abcdef" for ch in stripped)


def kill_switch_names() -> tuple[str, ...]:
    return tuple(field.name for field in fields(KillSwitches))


def capabilities(values: Iterable[str]) -> tuple[ActivationCapability, ...]:
    return tuple(ActivationCapability(value) for value in values)
