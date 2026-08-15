from __future__ import annotations

import asyncio
import uuid

import pytest

import app.services.organization_profile_mutation_service as service
from app.repositories.organization_registration_mutations import (
    CreatedOrganizationRegistration,
)


class _FakeBegin:
    def __init__(self, session: "_FakeSession") -> None:
        self.session = session

    async def __aenter__(self):
        assert not self.session.active
        self.session.active = True
        self.session.begin_entries += 1
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        self.session.active = False
        if exc_type is None:
            self.session.commits += 1
        else:
            self.session.rollbacks += 1
        return False


class _FakeSession:
    def __init__(self, *, already_active: bool = False) -> None:
        self.active = already_active
        self.begin_entries = 0
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        return self.active

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self)


def _registration(*, id_type: str = "PAN", country: str = "IN") -> CreatedOrganizationRegistration:
    return CreatedOrganizationRegistration(
        id=uuid.uuid4(),
        id_type=id_type,
        id_number_masked="XXXXXX1234",
        country_code=country,
        entity_type="P" if id_type == "PAN" else None,
        is_verified=False,
        verified_at=None,
    )


def _plan() -> service.RegistrationMutationPlan:
    return service.RegistrationMutationPlan(
        id_type="PAN",
        normalized_identifier="ABCDE1234F",
        masked_identifier="XXXXXX234F",
        country_code="IN",
        entity_type="D",
    )


def test_profile_only_uses_one_transaction_and_never_calls_registration_write(monkeypatch) -> None:
    session = _FakeSession()
    calls: list[str] = []

    async def update_profile(_session, patch):
        assert _session is session
        assert patch == {"name": "Atomic Gym"}
        assert session.active
        calls.append("profile-update")
        return {"id": uuid.uuid4(), "name": "Atomic Gym"}

    async def get_profile(_session):
        assert session.active
        calls.append("profile-read")
        return {"id": uuid.uuid4(), "name": "Atomic Gym"}

    async def list_regs(_session):
        assert session.active
        calls.append("registration-read")
        return []

    async def forbidden_write(*args, **kwargs):
        raise AssertionError("registration write must not run for profile-only patch")

    monkeypatch.setattr(service, "update_current_organization_profile", update_profile)
    monkeypatch.setattr(service, "get_current_organization_profile", get_profile)
    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", forbidden_write)
    monkeypatch.setattr(service, "replace_secure_organization_registration", forbidden_write)

    result = asyncio.run(
        service.mutate_organization_profile_atomically(
            session,
            profile_patch={"name": "Atomic Gym"},
            registration_updates=(),
        )
    )

    assert result.profile["name"] == "Atomic Gym"
    assert result.registrations == []
    assert session.begin_entries == 1
    assert session.commits == 1
    assert session.rollbacks == 0
    assert calls == ["profile-update", "profile-read", "registration-read"]


def test_combined_create_runs_registration_before_profile_and_commits_once(monkeypatch) -> None:
    session = _FakeSession()
    events: list[str] = []
    created = _registration()
    reads = 0

    async def update_profile(_session, patch):
        assert session.active
        events.append("profile-update")
        return {"id": uuid.uuid4(), "name": patch["name"]}

    async def get_profile(_session):
        assert session.active
        events.append("profile-final-read")
        return {"id": uuid.uuid4(), "name": "Atomic Gym"}

    async def list_regs(_session):
        nonlocal reads
        assert session.active
        reads += 1
        events.append(f"registration-read-{reads}")
        return [] if reads == 1 else [
            {
                "id": created.id,
                "id_type": created.id_type,
                "id_number_masked": created.id_number_masked,
                "country_code": created.country_code,
            }
        ]

    async def create_reg(_session, **kwargs):
        assert session.active
        assert kwargs["normalized_identifier"] == "ABCDE1234F"
        events.append("registration-create")
        return created

    async def forbidden_replace(*args, **kwargs):
        raise AssertionError("create path must not replace")

    monkeypatch.setattr(service, "update_current_organization_profile", update_profile)
    monkeypatch.setattr(service, "get_current_organization_profile", get_profile)
    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", create_reg)
    monkeypatch.setattr(service, "replace_secure_organization_registration", forbidden_replace)

    asyncio.run(
        service.mutate_organization_profile_atomically(
            session,
            profile_patch={"name": "Atomic Gym"},
            registration_updates=(_plan(),),
        )
    )

    assert session.begin_entries == 1
    assert session.commits == 1
    assert session.rollbacks == 0
    assert events == [
        "registration-read-1",
        "registration-create",
        "profile-update",
        "profile-final-read",
        "registration-read-2",
    ]


def test_registration_failure_occurs_before_profile_update_and_rolls_back(monkeypatch) -> None:
    session = _FakeSession()

    async def list_regs(_session):
        assert session.active
        return []

    async def fail_registration(_session, **kwargs):
        assert session.active
        raise RuntimeError("injected registration failure")

    async def forbidden_profile(*args, **kwargs):
        raise AssertionError("profile update must not run after registration failure")

    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", fail_registration)
    monkeypatch.setattr(service, "update_current_organization_profile", forbidden_profile)

    with pytest.raises(RuntimeError, match="injected registration failure"):
        asyncio.run(
            service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "must not run"},
                registration_updates=(_plan(),),
            )
        )

    assert session.begin_entries == 1
    assert session.commits == 0
    assert session.rollbacks == 1


def test_failure_after_registration_and_profile_mutations_rolls_back_both(monkeypatch) -> None:
    session = _FakeSession()
    created = _registration()
    registration_written = False
    profile_written = False

    async def list_regs(_session):
        assert session.active
        return []

    async def create_reg(_session, **kwargs):
        nonlocal registration_written
        assert session.active
        registration_written = True
        return created

    async def update_profile(_session, patch):
        nonlocal profile_written
        assert registration_written
        assert session.active
        profile_written = True
        return {"id": uuid.uuid4(), "name": patch["name"]}

    async def fail_final_profile_read(_session):
        assert registration_written and profile_written
        raise RuntimeError("injected final read failure")

    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", create_reg)
    monkeypatch.setattr(service, "update_current_organization_profile", update_profile)
    monkeypatch.setattr(service, "get_current_organization_profile", fail_final_profile_read)

    with pytest.raises(RuntimeError, match="injected final read failure"):
        asyncio.run(
            service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "must rollback"},
                registration_updates=(_plan(),),
            )
        )

    assert registration_written and profile_written
    assert session.commits == 0
    assert session.rollbacks == 1


def test_cancellation_after_both_mutations_rolls_back_and_propagates(monkeypatch) -> None:
    session = _FakeSession()
    created = _registration()
    registration_written = False
    profile_written = False

    async def list_regs(_session):
        assert session.active
        return []

    async def create_reg(_session, **kwargs):
        nonlocal registration_written
        registration_written = True
        return created

    async def update_profile(_session, patch):
        nonlocal profile_written
        assert registration_written
        profile_written = True
        return {"id": uuid.uuid4(), "name": patch["name"]}

    async def cancel_final_read(_session):
        assert registration_written and profile_written
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", create_reg)
    monkeypatch.setattr(service, "update_current_organization_profile", update_profile)
    monkeypatch.setattr(service, "get_current_organization_profile", cancel_final_read)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "must rollback"},
                registration_updates=(_plan(),),
            )
        )

    assert registration_written and profile_written
    assert session.commits == 0
    assert session.rollbacks == 1


def test_exact_existing_mask_is_rejected_without_guessing_mask_shape(monkeypatch) -> None:
    session = _FakeSession()
    existing_id = uuid.uuid4()
    exact_mask = "XXLEGIT1234"
    plan = service.RegistrationMutationPlan(
        id_type="BUSINESS_ID",
        normalized_identifier=exact_mask,
        masked_identifier="XXXXXXX1234",
        country_code="US",
        entity_type=None,
    )

    async def list_regs(_session):
        return [
            {
                "id": existing_id,
                "id_type": "BUSINESS_ID",
                "id_number_masked": exact_mask,
                "country_code": "US",
            }
        ]

    async def forbidden(*args, **kwargs):
        raise AssertionError("no mutation may run for exact masked resubmission")

    monkeypatch.setattr(service, "list_current_organization_registrations", list_regs)
    monkeypatch.setattr(service, "create_secure_organization_registration", forbidden)
    monkeypatch.setattr(service, "replace_secure_organization_registration", forbidden)
    monkeypatch.setattr(service, "update_current_organization_profile", forbidden)

    with pytest.raises(service.MaskedRegistrationIdentifierError):
        asyncio.run(
            service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "must not run"},
                registration_updates=(plan,),
            )
        )

    assert session.commits == 0
    assert session.rollbacks == 1


def test_preopened_transaction_is_rejected_before_any_domain_call(monkeypatch) -> None:
    session = _FakeSession(already_active=True)

    async def forbidden(*args, **kwargs):
        raise AssertionError("domain capability must not run")

    monkeypatch.setattr(service, "update_current_organization_profile", forbidden)
    monkeypatch.setattr(service, "get_current_organization_profile", forbidden)
    monkeypatch.setattr(service, "list_current_organization_registrations", forbidden)

    with pytest.raises(service.OrganizationProfileTransactionStateError):
        asyncio.run(
            service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "nope"},
                registration_updates=(),
            )
        )

    assert session.begin_entries == 0
    assert session.commits == 0
    assert session.rollbacks == 0
