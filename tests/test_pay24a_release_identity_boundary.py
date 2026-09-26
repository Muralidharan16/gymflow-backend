from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone

import pytest

from app.payment_activation.authority import (
    DurableActivationAuthority,
    MeasuredReleaseIdentity,
    TrustedReleaseIdentityUnavailable,
)
from app.payment_activation.domain import ActivationStage, KillSwitches


SHA_A = "a" * 40


class _NoDatabaseSession:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, *_args, **_kwargs):
        self.execute_calls += 1
        raise AssertionError("malformed or missing release identity reached SQL")


class _Provider:
    def __init__(self, value: object) -> None:
        self.value = value

    def measure(self) -> object:
        return self.value


async def _bind(authority: DurableActivationAuthority) -> None:
    await authority.bind_release_identity(
        operation_id=uuid.uuid4(),
        expected_generation=0,
        certified_sha=SHA_A,
        actor="synthetic-test-operator",
    )


def test_release_binding_has_no_caller_supplied_deployed_sha_parameter() -> None:
    parameters = inspect.signature(
        DurableActivationAuthority.bind_release_identity
    ).parameters
    assert "deployed_sha" not in parameters
    assert "certified_sha" in parameters


@pytest.mark.asyncio
async def test_missing_trusted_release_identity_fails_before_database_call() -> None:
    session = _NoDatabaseSession()
    authority = DurableActivationAuthority(session)  # type: ignore[arg-type]

    with pytest.raises(
        TrustedReleaseIdentityUnavailable,
        match="not configured",
    ):
        await _bind(authority)

    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "measurement",
    [
        None,
        object(),
        MeasuredReleaseIdentity(
            deployed_sha="A" * 40,
            measured_by="deployment-attestor",
            measured_at=datetime.now(timezone.utc),
        ),
        MeasuredReleaseIdentity(
            deployed_sha="a" * 39,
            measured_by="deployment-attestor",
            measured_at=datetime.now(timezone.utc),
        ),
        MeasuredReleaseIdentity(
            deployed_sha=SHA_A,
            measured_by="",
            measured_at=datetime.now(timezone.utc),
        ),
        MeasuredReleaseIdentity(
            deployed_sha=SHA_A,
            measured_by="deployment-attestor",
            measured_at=datetime.now(),
        ),
    ],
)
async def test_malformed_trusted_release_identity_fails_before_database_call(
    measurement: object,
) -> None:
    session = _NoDatabaseSession()
    authority = DurableActivationAuthority(  # type: ignore[arg-type]
        session,
        release_identity_provider=_Provider(measurement),  # type: ignore[arg-type]
    )

    with pytest.raises(
        TrustedReleaseIdentityUnavailable,
        match="malformed",
    ):
        await _bind(authority)

    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_stage", [True, 1, "1", None])
async def test_malformed_authorized_stage_fails_before_database_call(
    authorized_stage: object,
) -> None:
    session = _NoDatabaseSession()
    authority = DurableActivationAuthority(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="authorized_stage"):
        await authority.bind_human_authorization(
            operation_id=uuid.uuid4(),
            expected_generation=0,
            authorization_id="synthetic-stage1-auth",
            authorized_sha=SHA_A,
            authorized_stage=authorized_stage,  # type: ignore[arg-type]
            authorized_by="synthetic-test-authorizer",
            authorized_at=datetime.now(timezone.utc),
            actor="synthetic-test-operator",
        )

    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_stage", "switches"),
    [
        (True, KillSwitches()),
        (1, KillSwitches()),
        ("1", KillSwitches()),
        (
            ActivationStage.INTERNAL_ORGANIZATION,
            KillSwitches(checkout=1),  # type: ignore[arg-type]
        ),
    ],
)
async def test_malformed_transition_posture_fails_before_database_call(
    target_stage: object,
    switches: KillSwitches,
) -> None:
    session = _NoDatabaseSession()
    authority = DurableActivationAuthority(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await authority.transition_activation(
            operation_id=uuid.uuid4(),
            expected_generation=0,
            target_stage=target_stage,  # type: ignore[arg-type]
            provider_egress="blocked",
            internal_organization_id=None,
            kill_switches=switches,
            actor="synthetic-test-operator",
        )

    assert session.execute_calls == 0
