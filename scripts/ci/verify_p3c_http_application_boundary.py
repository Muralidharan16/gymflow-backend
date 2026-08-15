from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import contextmanager

import psycopg
from httpx import ASGITransport, AsyncClient

from app.core.aws_kms import AWSKMSUnavailableError, EncryptedDataKey
from app.core.security import create_access_token
from app.main import app
import app.services.registration_key_service as registration_key_service


ALPHA_ORG = uuid.UUID("41111111-1111-4111-8111-111111111111")
BETA_ORG = uuid.UUID("42222222-2222-4222-8222-222222222222")
ALPHA_OWNER = uuid.UUID("caaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BETA_OWNER = uuid.UUID("cbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PLAINTEXT_DEK = bytes(range(32))
WRAPPED_DEK = b"p3c-http-wrapped-registration-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3c-http"


@contextmanager
def _migration_connection():
    dsn = os.environ.get("P3C_HTTP_MIGRATION_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3C_HTTP_MIGRATION_DSN is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _set_context(cursor, *, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    for name, value in (
        ("app.current_org_id", str(org_id)),
        ("app.current_user_id", str(user_id)),
        ("app.current_user", str(user_id)),
        ("app.current_principal_type", "owner"),
        ("app.current_role", "owner"),
        ("app.current_gym_id", ""),
    ):
        cursor.execute("SELECT pg_catalog.set_config(%s, %s, true)", (name, value))


def _seed() -> None:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (
                        id, name, slug, tier, is_active, tagline
                    ) VALUES
                      (%s, 'P3C HTTP Alpha', 'p3c-http-alpha', 'basic', TRUE, 'alpha-before'),
                      (%s, 'P3C HTTP Beta', 'p3c-http-beta', 'basic', TRUE, 'beta-before')
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s, %s, 'P3C HTTP Alpha Owner',
                       'p3c-http-alpha@example.test', 'not-a-real-password', TRUE, FALSE),
                      (%s, %s, 'P3C HTTP Beta Owner',
                       'p3c-http-beta@example.test', 'not-a-real-password', TRUE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )


def _snapshot() -> tuple[str, str | None, list[tuple]]:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT name, tagline FROM public.organizations WHERE id = %s",
                    (ALPHA_ORG,),
                )
                row = cursor.fetchone()
                assert row is not None
                cursor.execute(
                    """
                    SELECT id_type, id_number_masked, country_code, is_verified,
                           verified_at, crypto_version
                    FROM public.organization_registrations
                    WHERE org_id = %s
                    ORDER BY id_type, country_code
                    """,
                    (ALPHA_ORG,),
                )
                registrations = cursor.fetchall()
                return str(row[0]), row[1], registrations


class _HTTPKMS:
    fail_decrypt = False
    generate_calls = 0
    decrypt_calls = 0

    async def generate_encrypted_data_key(self) -> EncryptedDataKey:
        type(self).generate_calls += 1
        return EncryptedDataKey(ciphertext=WRAPPED_DEK, key_id=WRAPPING_KEY_ID)

    async def decrypt_dek(self, encrypted_dek: bytes, wrapping_key_id: str) -> bytes:
        type(self).decrypt_calls += 1
        if type(self).fail_decrypt:
            raise AWSKMSUnavailableError("injected HTTP KMS outage")
        assert encrypted_dek == WRAPPED_DEK
        assert wrapping_key_id == WRAPPING_KEY_ID
        return PLAINTEXT_DEK


def _provider(_tenant_id: str) -> _HTTPKMS:
    return _HTTPKMS()


def _token(
    *,
    org_id: uuid.UUID = ALPHA_ORG,
    owner_id: uuid.UUID = ALPHA_OWNER,
    role: str = "owner",
) -> str:
    return create_access_token(
        owner_id=owner_id,
        org_id=org_id,
        email="p3c-http-alpha@example.test",
        role=role,
        principal_type="owner",
    )


def _headers(**kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(**kwargs)}"}


async def _run() -> None:
    registration_key_service.registration_kms_provider = _provider
    _HTTPKMS.fail_decrypt = False
    _HTTPKMS.generate_calls = 0
    _HTTPKMS.decrypt_calls = 0

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://p3c.test") as client:
        # Profile-only PATCH traverses the real middleware/dependency/router path.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"name": "P3C HTTP Profile Only", "tagline": "profile-only"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["name"] == "P3C HTTP Profile Only"
        assert body["business_id"] is None
        assert body["gst_number"] is None
        assert body["pan_number"] is None
        assert body["registrations"] == []
        assert _HTTPKMS.generate_calls == 0
        assert _HTTPKMS.decrypt_calls == 0

        # Combined HTTP mutation must return only masked registration metadata.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"name": "P3C HTTP Combined", "pan_number": "ABCDE1234F"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["name"] == "P3C HTTP Combined"
        assert body["pan_number"] is None
        assert body["business_id"] is None
        assert body["gst_number"] is None
        pan_rows = [item for item in body["registrations"] if item["id_type"] == "PAN"]
        assert len(pan_rows) == 1, body
        assert pan_rows[0]["id_number_masked"] == "XXXXXX234F"

        # Add a generic ID through the same PATCH so its server mask remains
        # syntactically valid input; exact resubmission must still be rejected.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"business_id": "US-BUSINESS-123456"},
        )
        assert response.status_code == 200, response.text
        business_rows = [
            item for item in response.json()["data"]["registrations"]
            if item["id_type"] == "BUSINESS_ID"
        ]
        assert len(business_rows) == 1
        business_mask = business_rows[0]["id_number_masked"]
        before_mask_retry = _snapshot()

        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"name": "MUST NOT COMMIT MASK", "business_id": business_mask},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == (
            "Masked organization registration identifiers cannot be submitted"
        )
        assert _snapshot() == before_mask_retry

        # Invalid public syntax fails before KMS/database mutation.
        kms_before_invalid = (_HTTPKMS.generate_calls, _HTTPKMS.decrypt_calls)
        before_invalid = _snapshot()
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"name": "MUST NOT COMMIT INVALID", "pan_number": "not-a-pan"},
        )
        assert response.status_code == 400, response.text
        assert _snapshot() == before_invalid
        assert (_HTTPKMS.generate_calls, _HTTPKMS.decrypt_calls) == kms_before_invalid

        # KMS outage maps to a generic 503 and cannot commit the profile change.
        before_kms = _snapshot()
        _HTTPKMS.fail_decrypt = True
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"name": "MUST NOT COMMIT KMS", "pan_number": "FGHIJ5678K"},
        )
        _HTTPKMS.fail_decrypt = False
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == (
            "Organization registration encryption service is unavailable"
        )
        assert "FGHIJ5678K" not in response.text
        assert _snapshot() == before_kms

        # Role authorization is rejected before the P3C business service.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(role="trainer"),
            json={"name": "MUST NOT COMMIT ROLE"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Organization admin access required"
        assert _snapshot() == before_kms

        # Cross-tenant token cannot mutate Alpha. Use profile-only so P3A is the
        # decisive authorization boundary and assert zero state change.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(org_id=BETA_ORG, owner_id=ALPHA_OWNER),
            json={"name": "MUST NOT COMMIT CROSS TENANT"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Organization profile access denied"
        assert _snapshot() == before_kms

        # Retry the same real identifier: P3B replacement policy may rotate the
        # ciphertext/reset verification, but P3C must not duplicate the business
        # registration row or leak plaintext in the response.
        response = await client.patch(
            "/organizations/profile",
            headers=_headers(),
            json={"pan_number": "ABCDE1234F"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        pan_rows = [item for item in body["registrations"] if item["id_type"] == "PAN"]
        assert len(pan_rows) == 1
        assert "ABCDE1234F" not in response.text
        snapshot_after_retry = _snapshot()
        pan_db_rows = [row for row in snapshot_after_retry[2] if row[0] == "PAN"]
        assert len(pan_db_rows) == 1

    print("P3C FastAPI profile + registration application boundary: PASS")


def main() -> None:
    _seed()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
