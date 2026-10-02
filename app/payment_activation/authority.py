"""Bounded PostgreSQL façade for PAY-24A durable activation authority.

The functions in ``app_secure`` are the security and concurrency boundary.
This module deliberately performs no provider I/O and never reads or mutates
the private authority tables directly.  It also does not commit transactions;
callers retain explicit transaction ownership around each database capability.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.payment_activation.domain import (
    ActivationCapability,
    ActivationStage,
    KillSwitches,
)


ProviderEgressState = Literal["blocked", "open", "closing"]
ProviderAdmissionState = Literal[
    "admitted",
    "active",
    "completed",
    "expired",
    "revoked",
    "unknown",
]
ProviderAdmissionOutcome = Literal["completed", "unknown"]


@dataclass(frozen=True, slots=True)
class MeasuredReleaseIdentity:
    """Release identity produced by a trusted deployment measurement source."""

    deployed_sha: str
    measured_by: str
    measured_at: datetime


@runtime_checkable
class TrustedReleaseIdentityProvider(Protocol):
    """Deployment-owned source of the release identity actually running.

    Production composition must provide an implementation backed by trusted
    deployment metadata or an equivalent attested source.  Synthetic providers
    are suitable only for local tests.  The durable façade intentionally has no
    public method that accepts a caller-supplied deployed SHA.  Repeated reads
    for one deployed artifact should return the same attestation identity; the
    database nevertheless keys an operation replay on SHA and measurer rather
    than on a newly sampled wall-clock timestamp.
    """

    def measure(
        self,
    ) -> MeasuredReleaseIdentity | Awaitable[MeasuredReleaseIdentity]: ...


class TrustedReleaseIdentityUnavailable(RuntimeError):
    """No complete trusted release measurement is available; fail closed."""


@dataclass(frozen=True, slots=True)
class ReleaseIdentityBinding:
    generation: int
    release_identity_id: uuid.UUID
    certified_sha: str
    deployed_sha: str


@dataclass(frozen=True, slots=True)
class HumanAuthorizationBinding:
    generation: int
    authorization_id: str
    authorized_stage: ActivationStage


@dataclass(frozen=True, slots=True)
class ActivationTransitionResult:
    generation: int
    stage: ActivationStage
    provider_egress: ProviderEgressState
    posture_digest: str


@dataclass(frozen=True, slots=True)
class EmergencyRollbackStarted:
    generation: int
    revoked_admission_count: int
    active_admission_count: int


@dataclass(frozen=True, slots=True)
class ProviderAdmissionExpiryResult:
    expired_count: int
    unknown_count: int


@dataclass(frozen=True, slots=True)
class EmergencyRollbackFinalized:
    generation: int
    stage: ActivationStage
    provider_egress: ProviderEgressState


@dataclass(frozen=True, slots=True)
class ActivationAuthoritySnapshot:
    stage: ActivationStage
    generation: int
    provider_egress: ProviderEgressState
    enabled_capabilities: tuple[ActivationCapability, ...]
    internal_organization_id: uuid.UUID | None
    authorization_id: str | None
    authorized_stage: ActivationStage | None
    certified_sha: str | None
    deployed_sha: str | None
    updated_at: datetime
    posture_digest: str


@dataclass(frozen=True, slots=True)
class ActivationTransitionEvidence:
    event_sequence: int
    event_id: uuid.UUID
    operation_id: uuid.UUID
    event_type: str
    prior_generation: int
    new_generation: int
    prior_stage: ActivationStage
    new_stage: ActivationStage
    authorization_id: str | None
    authorized_stage: ActivationStage | None
    certified_sha: str | None
    deployed_sha: str | None
    database_principal: str
    actor: str
    occurred_at: datetime
    posture_digest: str
    previous_event_hash: str | None
    event_hash: str
    event_details: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProviderAdmission:
    admission_id: uuid.UUID
    activation_generation: int
    organization_id: uuid.UUID
    capability: ActivationCapability
    logical_operation_id: str
    lease_expires_at: datetime
    state: ProviderAdmissionState
    execution_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderAdmissionFinished:
    admission_id: uuid.UUID
    state: ProviderAdmissionState
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderAdmissionDrainSnapshot:
    activation_generation: int
    state: ProviderAdmissionState
    lease_count: int
    oldest_lease_expiry: datetime | None
    newest_lease_expiry: datetime | None


_BIND_RELEASE_IDENTITY_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_bind_release_identity(
        CAST(:operation_id AS uuid),
        CAST(:expected_generation AS bigint),
        CAST(:certified_sha AS text),
        CAST(:deployed_sha AS text),
        CAST(:measured_by AS text),
        CAST(:measured_at AS timestamptz),
        CAST(:actor AS text)
    )
    """
)

_BIND_HUMAN_AUTHORIZATION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_bind_human_authorization(
        CAST(:operation_id AS uuid),
        CAST(:expected_generation AS bigint),
        CAST(:authorization_id AS text),
        CAST(:authorized_sha AS text),
        CAST(:authorized_stage AS smallint),
        CAST(:authorized_by AS text),
        CAST(:authorized_at AS timestamptz),
        CAST(:actor AS text)
    )
    """
)

_TRANSITION_ACTIVATION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_transition_activation(
        CAST(:operation_id AS uuid),
        CAST(:expected_generation AS bigint),
        CAST(:target_stage AS smallint),
        CAST(:provider_egress AS text),
        CAST(:internal_organization_id AS uuid),
        CAST(:checkout AS boolean),
        CAST(:webhooks AS boolean),
        CAST(:payment_application AS boolean),
        CAST(:subscription_activation AS boolean),
        CAST(:refund_execution AS boolean),
        CAST(:recurring_billing AS boolean),
        CAST(:dunning AS boolean),
        CAST(:platform_billing AS boolean),
        CAST(:actor AS text)
    )
    """
)

_BEGIN_EMERGENCY_ROLLBACK_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_begin_emergency_rollback(
        CAST(:operation_id AS uuid),
        CAST(:expected_generation AS bigint),
        CAST(:actor AS text)
    )
    """
)

_EXPIRE_PROVIDER_ADMISSIONS_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_expire_provider_admissions(
        CAST(:operation_id AS uuid),
        CAST(:actor AS text)
    )
    """
)

_FINALIZE_EMERGENCY_ROLLBACK_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_finalize_emergency_rollback(
        CAST(:operation_id AS uuid),
        CAST(:expected_generation AS bigint),
        CAST(:actor AS text)
    )
    """
)

_ACTIVATION_SNAPSHOT_SQL = text(
    "SELECT * FROM app_secure.pay24a_activation_snapshot()"
)

_TRANSITION_EVIDENCE_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_transition_evidence(
        CAST(:after_event_sequence AS bigint),
        CAST(:limit AS integer)
    )
    """
)

_REQUEST_PROVIDER_ADMISSION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_request_provider_admission(
        CAST(:expected_generation AS bigint),
        CAST(:capability AS text),
        CAST(:logical_operation_id AS text),
        CAST(:operation_sha AS text),
        CAST(:lease_seconds AS integer)
    )
    """
)

_REQUEST_CURRENT_PROVIDER_ADMISSION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24b_request_current_provider_admission(
        CAST(:capability AS text),
        CAST(:logical_operation_id AS text),
        CAST(:operation_sha AS text),
        CAST(:lease_seconds AS integer)
    )
    """
)

_REQUEST_CURRENT_REFUND_ADMISSION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24b_request_current_refund_admission(
        CAST(:logical_operation_id AS text),
        CAST(:operation_sha AS text),
        CAST(:lease_seconds AS integer)
    )
    """
)

_START_PROVIDER_ADMISSION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_start_provider_admission(
        CAST(:admission_id AS uuid),
        CAST(:execution_id AS uuid)
    )
    """
)

_FINISH_PROVIDER_ADMISSION_SQL = text(
    """
    SELECT *
    FROM app_secure.pay24a_finish_provider_admission(
        CAST(:admission_id AS uuid),
        CAST(:execution_id AS uuid),
        CAST(:outcome AS text)
    )
    """
)

_ADMISSION_DRAIN_SNAPSHOT_SQL = text(
    "SELECT * FROM app_secure.pay24a_admission_drain_snapshot()"
)


class DurableActivationAuthority:
    """Call the least-privilege PAY-24A PostgreSQL capabilities.

    Database role ACLs decide which methods a given session may successfully
    invoke.  Organization identity for provider admission is resolved inside
    PostgreSQL from the authenticated session context; it is intentionally not
    accepted as a method argument.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        release_identity_provider: TrustedReleaseIdentityProvider | None = None,
    ) -> None:
        self._session = session
        self._release_identity_provider = release_identity_provider

    async def bind_release_identity(
        self,
        *,
        operation_id: uuid.UUID,
        expected_generation: int,
        certified_sha: str,
        actor: str,
    ) -> ReleaseIdentityBinding:
        """Bind certified SHA to a freshly measured deployed identity.

        There is deliberately no ``deployed_sha`` argument.  The value passed
        to PostgreSQL can only come from the provider installed at composition
        time, and a missing or malformed measurement fails closed.
        """

        measured = await self._measure_release_identity()
        result = await self._session.execute(
            _BIND_RELEASE_IDENTITY_SQL,
            {
                "operation_id": operation_id,
                "expected_generation": expected_generation,
                "certified_sha": certified_sha,
                "deployed_sha": measured.deployed_sha,
                "measured_by": measured.measured_by,
                "measured_at": measured.measured_at,
                "actor": actor,
            },
        )
        row = result.mappings().one()
        return ReleaseIdentityBinding(
            generation=int(row["generation"]),
            release_identity_id=row["release_identity_id"],
            certified_sha=str(row["certified_sha"]),
            deployed_sha=str(row["deployed_sha"]),
        )

    async def bind_human_authorization(
        self,
        *,
        operation_id: uuid.UUID,
        expected_generation: int,
        authorization_id: str,
        authorized_sha: str,
        authorized_stage: ActivationStage,
        authorized_by: str,
        authorized_at: datetime,
        actor: str,
    ) -> HumanAuthorizationBinding:
        _require_pay24a_stage(authorized_stage, "authorized_stage")
        result = await self._session.execute(
            _BIND_HUMAN_AUTHORIZATION_SQL,
            {
                "operation_id": operation_id,
                "expected_generation": expected_generation,
                "authorization_id": authorization_id,
                "authorized_sha": authorized_sha,
                "authorized_stage": int(authorized_stage),
                "authorized_by": authorized_by,
                "authorized_at": authorized_at,
                "actor": actor,
            },
        )
        row = result.mappings().one()
        return HumanAuthorizationBinding(
            generation=int(row["generation"]),
            authorization_id=str(row["authorization_id"]),
            authorized_stage=ActivationStage(int(row["authorized_stage"])),
        )

    async def transition_activation(
        self,
        *,
        operation_id: uuid.UUID,
        expected_generation: int,
        target_stage: ActivationStage,
        provider_egress: ProviderEgressState,
        internal_organization_id: uuid.UUID | None,
        kill_switches: KillSwitches,
        actor: str,
    ) -> ActivationTransitionResult:
        _require_pay24a_stage(target_stage, "target_stage")
        if type(provider_egress) is not str or provider_egress not in {
            "blocked",
            "open",
            "closing",
        }:
            raise ValueError("provider_egress is invalid")
        if (
            type(kill_switches) is not KillSwitches
            or kill_switches.invalid_value_names()
        ):
            raise ValueError("kill_switches must contain exact boolean values")
        result = await self._session.execute(
            _TRANSITION_ACTIVATION_SQL,
            {
                "operation_id": operation_id,
                "expected_generation": expected_generation,
                "target_stage": int(target_stage),
                "provider_egress": provider_egress,
                "internal_organization_id": internal_organization_id,
                "checkout": kill_switches.checkout,
                "webhooks": kill_switches.webhooks,
                "payment_application": kill_switches.payment_application,
                "subscription_activation": (
                    kill_switches.subscription_activation
                ),
                "refund_execution": kill_switches.refund_execution,
                "recurring_billing": kill_switches.recurring_billing,
                "dunning": kill_switches.dunning,
                "platform_billing": kill_switches.platform_billing,
                "actor": actor,
            },
        )
        row = result.mappings().one()
        return ActivationTransitionResult(
            generation=int(row["generation"]),
            stage=ActivationStage(int(row["stage"])),
            provider_egress=str(row["provider_egress"]),
            posture_digest=str(row["posture_digest"]),
        )

    async def begin_emergency_rollback(
        self,
        *,
        operation_id: uuid.UUID,
        expected_generation: int,
        actor: str,
    ) -> EmergencyRollbackStarted:
        """Close admission first and fence the prior activation generation."""

        result = await self._session.execute(
            _BEGIN_EMERGENCY_ROLLBACK_SQL,
            {
                "operation_id": operation_id,
                "expected_generation": expected_generation,
                "actor": actor,
            },
        )
        row = result.mappings().one()
        return EmergencyRollbackStarted(
            generation=int(row["generation"]),
            revoked_admission_count=int(row["revoked_admission_count"]),
            active_admission_count=int(row["active_admission_count"]),
        )

    async def expire_provider_admissions(
        self,
        *,
        operation_id: uuid.UUID,
        actor: str,
    ) -> ProviderAdmissionExpiryResult:
        result = await self._session.execute(
            _EXPIRE_PROVIDER_ADMISSIONS_SQL,
            {"operation_id": operation_id, "actor": actor},
        )
        row = result.mappings().one()
        return ProviderAdmissionExpiryResult(
            expired_count=int(row["expired_count"]),
            unknown_count=int(row["unknown_count"]),
        )

    async def finalize_emergency_rollback(
        self,
        *,
        operation_id: uuid.UUID,
        expected_generation: int,
        actor: str,
    ) -> EmergencyRollbackFinalized:
        """Finalize Stage 0 only after PostgreSQL proves the drain complete."""

        result = await self._session.execute(
            _FINALIZE_EMERGENCY_ROLLBACK_SQL,
            {
                "operation_id": operation_id,
                "expected_generation": expected_generation,
                "actor": actor,
            },
        )
        row = result.mappings().one()
        return EmergencyRollbackFinalized(
            generation=int(row["generation"]),
            stage=ActivationStage(int(row["stage"])),
            provider_egress=str(row["provider_egress"]),
        )

    async def snapshot(self) -> ActivationAuthoritySnapshot:
        result = await self._session.execute(_ACTIVATION_SNAPSHOT_SQL)
        row = result.mappings().one()
        authorized_stage = row["authorized_stage"]
        return ActivationAuthoritySnapshot(
            stage=ActivationStage(int(row["stage"])),
            generation=int(row["generation"]),
            provider_egress=str(row["provider_egress"]),
            enabled_capabilities=tuple(
                ActivationCapability(value)
                for value in row["enabled_capabilities"]
            ),
            internal_organization_id=row["internal_organization_id"],
            authorization_id=row["authorization_id"],
            authorized_stage=(
                ActivationStage(int(authorized_stage))
                if authorized_stage is not None
                else None
            ),
            certified_sha=row["certified_sha"],
            deployed_sha=row["deployed_sha"],
            updated_at=row["updated_at"],
            posture_digest=str(row["posture_digest"]),
        )

    async def transition_evidence(
        self,
        *,
        after_event_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[ActivationTransitionEvidence, ...]:
        """Read a bounded, non-secret projection of immutable evidence."""

        result = await self._session.execute(
            _TRANSITION_EVIDENCE_SQL,
            {
                "after_event_sequence": after_event_sequence,
                "limit": limit,
            },
        )
        evidence: list[ActivationTransitionEvidence] = []
        for row in result.mappings():
            authorized_stage = row["authorized_stage"]
            evidence.append(
                ActivationTransitionEvidence(
                    event_sequence=int(row["event_sequence"]),
                    event_id=row["event_id"],
                    operation_id=row["operation_id"],
                    event_type=str(row["event_type"]),
                    prior_generation=int(row["prior_generation"]),
                    new_generation=int(row["new_generation"]),
                    prior_stage=ActivationStage(int(row["prior_stage"])),
                    new_stage=ActivationStage(int(row["new_stage"])),
                    authorization_id=row["authorization_id"],
                    authorized_stage=(
                        ActivationStage(int(authorized_stage))
                        if authorized_stage is not None
                        else None
                    ),
                    certified_sha=row["certified_sha"],
                    deployed_sha=row["deployed_sha"],
                    database_principal=str(row["database_principal"]),
                    actor=str(row["actor"]),
                    occurred_at=row["occurred_at"],
                    posture_digest=str(row["posture_digest"]),
                    previous_event_hash=row["previous_event_hash"],
                    event_hash=str(row["event_hash"]),
                    event_details=dict(row["event_details"]),
                )
            )
        return tuple(evidence)

    async def admission_drain_snapshot(
        self,
    ) -> tuple[ProviderAdmissionDrainSnapshot, ...]:
        """Return aggregate lease counts used to decide rollback draining."""

        result = await self._session.execute(_ADMISSION_DRAIN_SNAPSHOT_SQL)
        return tuple(
            ProviderAdmissionDrainSnapshot(
                activation_generation=int(row["activation_generation"]),
                state=str(row["state"]),
                lease_count=int(row["lease_count"]),
                oldest_lease_expiry=row["oldest_lease_expiry"],
                newest_lease_expiry=row["newest_lease_expiry"],
            )
            for row in result.mappings()
        )

    async def request_provider_admission(
        self,
        *,
        expected_generation: int,
        capability: ActivationCapability,
        logical_operation_id: str,
        operation_sha: str,
        lease_seconds: int,
    ) -> ProviderAdmission:
        """Request one durable admission for the session-bound organization."""

        if type(capability) is not ActivationCapability:
            raise ValueError("capability is invalid")

        result = await self._session.execute(
            _REQUEST_PROVIDER_ADMISSION_SQL,
            {
                "expected_generation": expected_generation,
                "capability": capability.value,
                "logical_operation_id": logical_operation_id,
                "operation_sha": operation_sha,
                "lease_seconds": lease_seconds,
            },
        )
        return _provider_admission_from_row(result.mappings().one())

    async def request_current_provider_admission(
        self,
        *,
        capability: ActivationCapability,
        logical_operation_id: str,
        operation_sha: str,
        lease_seconds: int,
    ) -> ProviderAdmission:
        """Request a PAY-24-B admission against the current durable generation.

        PostgreSQL resolves and fences the current generation inside the bounded
        PAY-24-B checkout bridge.  The bridge remains fail-closed and delegates
        all Stage/egress/tenant/release/capability checks to PAY-24-A.
        """

        if type(capability) is not ActivationCapability:
            raise ValueError("capability is invalid")

        result = await self._session.execute(
            _REQUEST_CURRENT_PROVIDER_ADMISSION_SQL,
            {
                "capability": capability.value,
                "logical_operation_id": logical_operation_id,
                "operation_sha": operation_sha,
                "lease_seconds": lease_seconds,
            },
        )
        return _provider_admission_from_row(result.mappings().one())

    async def request_current_refund_admission(
        self,
        *,
        capability: ActivationCapability,
        logical_operation_id: str,
        operation_sha: str,
        lease_seconds: int,
    ) -> ProviderAdmission:
        """Request current-generation refund-execution admission.

        The separate PAY-24-B refund bridge accepts no caller-controlled
        capability and is executable only by ``finance_refund_runtime``.
        Requiring the matching enum here keeps application composition explicit
        while PostgreSQL remains the authoritative capability/identity fence.
        """

        if capability is not ActivationCapability.REFUND_EXECUTION:
            raise ValueError("capability must be refund_execution")

        result = await self._session.execute(
            _REQUEST_CURRENT_REFUND_ADMISSION_SQL,
            {
                "logical_operation_id": logical_operation_id,
                "operation_sha": operation_sha,
                "lease_seconds": lease_seconds,
            },
        )
        return _provider_admission_from_row(result.mappings().one())

    async def start_provider_admission(
        self,
        *,
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
    ) -> ProviderAdmission:
        """Mark an admission active; commit before any provider HTTP/I/O.

        This facade deliberately does not commit. A later provider integration
        must commit this short transaction before performing an external side
        effect, then record the terminal outcome in a subsequent transaction.
        """

        result = await self._session.execute(
            _START_PROVIDER_ADMISSION_SQL,
            {
                "admission_id": admission_id,
                "execution_id": execution_id,
            },
        )
        return _provider_admission_from_row(result.mappings().one())

    async def finish_provider_admission(
        self,
        *,
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
        outcome: ProviderAdmissionOutcome,
    ) -> ProviderAdmissionFinished:
        result = await self._session.execute(
            _FINISH_PROVIDER_ADMISSION_SQL,
            {
                "admission_id": admission_id,
                "execution_id": execution_id,
                "outcome": outcome,
            },
        )
        row = result.mappings().one()
        return ProviderAdmissionFinished(
            admission_id=row["admission_id"],
            state=str(row["state"]),
            finished_at=row["finished_at"],
        )

    async def _measure_release_identity(self) -> MeasuredReleaseIdentity:
        provider = self._release_identity_provider
        if provider is None:
            raise TrustedReleaseIdentityUnavailable(
                "trusted release identity provider is not configured"
            )

        measured_or_awaitable = provider.measure()
        if inspect.isawaitable(measured_or_awaitable):
            measured = await measured_or_awaitable
        else:
            measured = measured_or_awaitable

        if not _measurement_is_complete(measured):
            raise TrustedReleaseIdentityUnavailable(
                "trusted release identity measurement is malformed"
            )
        return measured


def _provider_admission_from_row(row) -> ProviderAdmission:
    return ProviderAdmission(
        admission_id=row["admission_id"],
        activation_generation=int(row["activation_generation"]),
        organization_id=row["organization_id"],
        capability=ActivationCapability(row["capability"]),
        logical_operation_id=str(row["logical_operation_id"]),
        lease_expires_at=row["lease_expires_at"],
        state=str(row["state"]),
        execution_id=(row["execution_id"] if "execution_id" in row else None),
    )


def _measurement_is_complete(value: object) -> bool:
    if type(value) is not MeasuredReleaseIdentity:
        return False
    if not (
        type(value.deployed_sha) is str
        and len(value.deployed_sha) == 40
        and all(ch in "0123456789abcdef" for ch in value.deployed_sha)
    ):
        return False
    if type(value.measured_by) is not str or not value.measured_by.strip():
        return False
    if (
        not isinstance(value.measured_at, datetime)
        or value.measured_at.tzinfo is None
    ):
        return False
    try:
        return value.measured_at.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _require_pay24a_stage(value: object, field_name: str) -> None:
    if type(value) is not ActivationStage or value not in {
        ActivationStage.LIVE_CONFIG_EGRESS_BLOCKED,
        ActivationStage.INTERNAL_ORGANIZATION,
    }:
        raise ValueError(f"{field_name} is not a PAY-24-A stage")
