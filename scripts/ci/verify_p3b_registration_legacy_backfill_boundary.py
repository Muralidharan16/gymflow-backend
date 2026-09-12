from __future__ import annotations

import os
import struct
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ALPHA_ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
BETA_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
ALPHA_OWNER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BETA_OWNER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
REGISTRATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000021")
BRANCH_ID = uuid.UUID("30000000-0000-4000-8000-000000000021")
LEGACY_CIPHERTEXT = "legacy-fernet-backfill-ciphertext"
MASK = "XXXXXX1234"
VERIFIED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
WRAPPED_DEK = b"kms-wrapped-registration-backfill-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3b-backfill"
PLAINTEXT_DEK = bytes(range(32))


@contextmanager
def _connection(env_name: str):
    dsn = os.environ.get(env_name, "").strip()
    if not dsn:
        raise RuntimeError(f"{env_name} is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _set_context(
    cursor,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    gym_id: uuid.UUID | None = None,
) -> None:
    for name, value in (
        ("app.current_org_id", str(org_id)),
        ("app.current_user_id", str(user_id)),
        ("app.current_principal_type", "owner"),
        ("app.current_role", "owner"),
        ("app.current_gym_id", "" if gym_id is None else str(gym_id)),
    ):
        cursor.execute("SELECT pg_catalog.set_config(%s, %s, true)", (name, value))


def _seed() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES
                      (%s, 'Alpha Backfill Gym', 'alpha-backfill-gym', 'basic', TRUE),
                      (%s, 'Beta Backfill Gym', 'beta-backfill-gym', 'basic', TRUE)
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s, %s, 'Alpha Owner', 'alpha-backfill@example.test',
                       'not-a-real-password', FALSE, FALSE),
                      (%s, %s, 'Beta Owner', 'beta-backfill@example.test',
                       'not-a-real-password', FALSE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    INSERT INTO public.organization_registrations (
                        id, org_id, id_type, id_number_encrypted,
                        id_number_masked, country_code, entity_type,
                        crypto_version, is_verified, verified_at
                    ) VALUES (%s, %s, 'PAN', %s, %s, 'IN', 'P', 0, TRUE, %s)
                    """,
                    (REGISTRATION_ID, ALPHA_ORG, LEGACY_CIPHERTEXT, MASK, VERIFIED_AT),
                )


def _legacy_rows(org_id: uuid.UUID, owner_id: uuid.UUID, gym_id=None):
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=org_id, user_id=owner_id, gym_id=gym_id)
                cursor.execute(
                    "SELECT * FROM app_secure.current_legacy_registration_backfill_rows()"
                )
                return cursor.fetchall()


def _install_key() -> int:
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT * FROM app_secure.install_registration_dek(%s, %s)",
                    (WRAPPED_DEK, WRAPPING_KEY_ID),
                )
                row = cursor.fetchone()
                assert row is not None
                return int(row[0])


def _envelope(key_version: int) -> bytes:
    aad = b"\x00".join(
        (
            b"doers:p3b:registration-envelope:v1",
            ALPHA_ORG.bytes,
            b"organization_registrations",
            REGISTRATION_ID.bytes,
            struct.pack(">I", key_version),
        )
    )
    nonce = bytes(range(12))
    return (
        struct.pack(">I", key_version)
        + nonce
        + AESGCM(PLAINTEXT_DEK).encrypt(nonce, b"ABCDE1234F", aad)
    )


def _convert(payload: bytes, key_version: int):
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    SELECT *
                    FROM app_secure.convert_legacy_organization_registration_envelope(
                        %s, %s, %s
                    )
                    """,
                    (REGISTRATION_ID, payload, key_version),
                )
                return cursor.fetchone()


def _assert_sqlstate(expected: str, action) -> None:
    try:
        action()
    except psycopg.Error as exc:
        assert exc.sqlstate == expected, exc
    else:
        raise AssertionError(f"expected SQLSTATE {expected}")


def _assert_persisted(payload: bytes, key_version: int) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    SELECT id_number_encrypted, id_number_masked, crypto_version,
                           is_verified, verified_at
                    FROM public.organization_registrations
                    WHERE id = %s
                    """,
                    (REGISTRATION_ID,),
                )
                assert cursor.fetchone() == (None, MASK, 1, True, VERIFIED_AT)
                cursor.execute(
                    """
                    SELECT tenant_id::text, payload_encrypted, key_version,
                           key_scope, schema_version
                    FROM public.organization_registration_payloads_secure
                    WHERE registration_id = %s
                    """,
                    (REGISTRATION_ID,),
                )
                assert cursor.fetchone() == (
                    str(ALPHA_ORG),
                    payload,
                    key_version,
                    "organization_registrations",
                    1,
                )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM public.p3b_registration_envelope_rows WHERE registration_id = %s",
                (REGISTRATION_ID,),
            )
            assert cursor.fetchone()[0] == 1


def main() -> None:
    _seed()

    own_rows = _legacy_rows(ALPHA_ORG, ALPHA_OWNER)
    assert own_rows == [
        (REGISTRATION_ID, "PAN", LEGACY_CIPHERTEXT, MASK, "IN")
    ]
    assert _legacy_rows(BETA_ORG, BETA_OWNER) == []
    _assert_sqlstate(
        "42501",
        lambda: _legacy_rows(ALPHA_ORG, ALPHA_OWNER, BRANCH_ID),
    )

    key_version = _install_key()
    payload = _envelope(key_version)
    converted = _convert(payload, key_version)
    assert converted == (
        REGISTRATION_ID,
        "PAN",
        MASK,
        "IN",
        True,
        VERIFIED_AT,
    )
    _assert_persisted(payload, key_version)
    assert _legacy_rows(ALPHA_ORG, ALPHA_OWNER) == []

    _assert_sqlstate("P0002", lambda: _convert(payload, key_version))
    _assert_persisted(payload, key_version)

    print("P3B legacy registration backfill runtime boundary: PASS")


if __name__ == "__main__":
    main()
