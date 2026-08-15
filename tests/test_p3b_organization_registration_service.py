from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import app.services.organization_registration_service as service


REGISTRATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")


def test_service_encrypts_with_preselected_record_id_before_database_create(monkeypatch) -> None:
    events: list[tuple] = []

    async def fake_encrypt(session, *, registration_id, normalized_identifier):
        events.append(("encrypt", session, registration_id, normalized_identifier))
        return SimpleNamespace(payload=b"ciphertext", key_version=7)

    async def fake_create(session, **kwargs):
        events.append(("create", session, kwargs))
        return "created"

    monkeypatch.setattr(service, "encrypt_current_registration_identifier", fake_encrypt)
    monkeypatch.setattr(service, "create_organization_registration_envelope", fake_create)

    session = object()
    result = asyncio.run(
        service.create_secure_organization_registration(
            session,
            id_type="PAN",
            normalized_identifier="ABCDE1234F",
            masked_identifier="XXXXXX1234",
            country_code="IN",
            entity_type="P",
            registration_id=REGISTRATION_ID,
        )
    )

    assert result == "created"
    assert events[0] == (
        "encrypt",
        session,
        REGISTRATION_ID,
        "ABCDE1234F",
    )
    assert events[1] == (
        "create",
        session,
        {
            "registration_id": REGISTRATION_ID,
            "id_type": "PAN",
            "id_number_masked": "XXXXXX1234",
            "country_code": "IN",
            "entity_type": "P",
            "payload_encrypted": b"ciphertext",
            "key_version": 7,
        },
    )


def test_service_generates_record_id_before_encryption_when_not_supplied(monkeypatch) -> None:
    generated = uuid.UUID("20000000-0000-4000-8000-000000000001")
    captured = {}

    monkeypatch.setattr(service.uuid, "uuid4", lambda: generated)

    async def fake_encrypt(session, *, registration_id, normalized_identifier):
        captured["encrypt_id"] = registration_id
        return SimpleNamespace(payload=b"ciphertext", key_version=11)

    async def fake_create(session, **kwargs):
        captured["create_id"] = kwargs["registration_id"]
        return kwargs["registration_id"]

    monkeypatch.setattr(service, "encrypt_current_registration_identifier", fake_encrypt)
    monkeypatch.setattr(service, "create_organization_registration_envelope", fake_create)

    result = asyncio.run(
        service.create_secure_organization_registration(
            object(),
            id_type="GST",
            normalized_identifier="29ABCDE1234F1Z5",
            masked_identifier="XXXXXXXXXXX1Z5",
            country_code="IN",
            entity_type=None,
        )
    )

    assert result == generated
    assert captured == {"encrypt_id": generated, "create_id": generated}


def test_service_does_not_normalize_or_mask_domain_identifiers_itself() -> None:
    source = service.__file__
    text = open(source, encoding="utf-8").read()

    assert "normalized_identifier" in text
    assert "masked_identifier" in text
    for forbidden in (
        ".upper()",
        ".lower()",
        ".replace(",
        "re.sub",
        "Fernet",
        "SECRET_KEY",
    ):
        assert forbidden not in text
