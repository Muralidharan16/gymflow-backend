from __future__ import annotations

import os
import struct
import uuid
from contextlib import contextmanager

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ALPHA_ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
BETA_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
ALPHA_OWNER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BETA_OWNER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PRIMARY_REG = uuid.UUID("10000000-0000-4000-8000-000000000001")
DUPLICATE_REG = uuid.UUID("10000000-0000-4000-8000-000000000002")
BAD_HEADER_REG = uuid.UUID("10000000-0000-4000-8000-000000000003")
MISSING_KEY_REG = uuid.UUID("10000000-0000-4000-8000-000000000004")
LOWERCASE_REG = uuid.UUID("10000000-0000-4000-8000-000000000005")
CROSS_TENANT_REG = uuid.UUID("10000000-0000-4000-8000-000000000006")
BRANCH_REG = uuid.UUID("10000000-0000-4000-8000-000000000007")
BRANCH_ID = uuid.UUID("30000000-0000-4000-8000-000000000001")
WRAPPED_DEK = b"kms-wrapped-registration-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3b-create"
PLAINTEXT_DEK = bytes(range(32))
IDENTIFIER = "ABCDE1234F"
MASK = "XXXXXX1234"


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
    principal_type: str = "owner",
    role: str = "owner",
    gym_id: uuid.UUID | None = None,
) -> None:
    values = (
        ("app.current_org_id", str(org_id)),
        ("app.current_user_id", str(user_id)),
        ("app.current_principal_type", principal_type),
        ("app.current_role", role),
        ("app.current_gym_id", "" if gym_id is None else str(gym_id)),
    )
    for name, value in values:
        cursor.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)",
            (name, value),
        )


def _seed_principals() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES
                      (%s, 'Alpha Create Gym', 'alpha-create-gym', 'basic', TRUE),
                      (%s, 'Beta Create Gym', 'beta-create-gym', 'basic', TRUE)
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s, %s, 'Alpha Owner',
                       'alpha-create-owner@example.test', 'not-a-real-password', FALSE, FALSE),
                      (%s, %s, 'Beta Owner',
                       'beta-create-owner@example.test', 'not-a-real-password', FALSE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )


def _install_alpha_key() -> int:
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
                assert row[1:] == (WRAPPED_DEK, WRAPPING_KEY_ID)
                return int(row[0])


def _envelope(
    *,
    registration_id: uuid.UUID,
    key_version: int,
    tenant_id: uuid.UUID = ALPHA_ORG,
    identifier: str = IDENTIFIER,
) -> bytes:
    aad = b"\x00".join(
        (
            b"doers:p3b:registration-envelope:v1",
            tenant_id.bytes,
            b"organization_registrations",
            registration_id.bytes,
            struct.pack(">I", key_version),
        )
    )
    nonce = bytes(range(12))
    ciphertext = AESGCM(PLAINTEXT_DEK).encrypt(
        nonce,
        identifier.encode("utf-8"),
        aad,
    )
    return struct.pack(">I", key_version) + nonce + ciphertext


def _call_create(
    *,
    registration_id: uuid.UUID,
    key_version: int,
    payload: bytes,
    org_id: uuid.UUID = ALPHA_ORG,
    user_id: uuid.UUID = ALPHA_OWNER,
    role: str = "owner",
    gym_id: uuid.UUID | None = None,
    id_type: str = "PAN",
    country_code: str = "IN",
    mask: str = MASK,
    entity_type: str | None = "P",
):
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(
                    cursor,
                    org_id=org_id,
                    user_id=user_id,
                    role=role,
                    gym_id=gym_id,
                )
                cursor.execute(
                    """
                    SELECT *
                    FROM app_secure.create_organization_registration_envelope(
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        registration_id,
                        id_type,
                        mask,
                        country_code,
                        entity_type,
                        payload,
                        key_version,
                    ),
                )
                return cursor.fetchone()


def _expect_sqlstate(expected: str, **kwargs) -> None:
    try:
        _call_create(**kwargs)
    except psycopg.Error as exc:
        if exc.sqlstate != expected:
            raise AssertionError(
                f"expected SQLSTATE {expected}, observed {exc.sqlstate}: {exc}"
            ) from exc
    else:
        raise AssertionError(f"registration create unexpectedly succeeded: {kwargs}")


def _assert_registration_absent(registration_id: uuid.UUID) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT count(*) FROM public.organization_registrations WHERE id = %s",
                    (registration_id,),
                )
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.organization_registration_payloads_secure
                    WHERE registration_id = %s
                    """,
                    (registration_id,),
                )
                assert cursor.fetchone()[0] == 0
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM public.p3b_registration_envelope_rows
                WHERE registration_id = %s
                """,
                (registration_id,),
            )
            assert cursor.fetchone()[0] == 0


def _assert_primary_persisted(key_version: int, payload: bytes) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    SELECT id::text, org_id::text, id_type,
                           id_number_encrypted, id_number_masked,
                           country_code, entity_type, crypto_version,
                           is_verified, verified_at
                    FROM public.organization_registrations
                    WHERE id = %s
                    """,
                    (PRIMARY_REG,),
                )
                assert cursor.fetchone() == (
                    str(PRIMARY_REG),
                    str(ALPHA_ORG),
                    "PAN",
                    None,
                    MASK,
                    "IN",
                    "P",
                    1,
                    False,
                    None,
                )
                cursor.execute(
                    """
                    SELECT registration_id::text, tenant_id::text,
                           payload_encrypted, key_version, key_scope, schema_version
                    FROM public.organization_registration_payloads_secure
                    WHERE registration_id = %s
                    """,
                    (PRIMARY_REG,),
                )
                assert cursor.fetchone() == (
                    str(PRIMARY_REG),
                    str(ALPHA_ORG),
                    payload,
                    key_version,
                    "organization_registrations",
                    1,
                )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT registration_id::text
                FROM public.p3b_registration_envelope_rows
                WHERE registration_id = %s
                """,
                (PRIMARY_REG,),
            )
            assert cursor.fetchone() == (str(PRIMARY_REG),)


def _assert_masked_read() -> None:
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT * FROM app_secure.current_organization_registrations()"
                )
                rows = cursor.fetchall()
                assert len(rows) == 1
                assert str(rows[0][0]) == str(PRIMARY_REG)
                assert rows[0][1] == "PAN"
                assert rows[0][2] == MASK
                assert rows[0][3] == "IN"
                assert rows[0][4] is False
                assert rows[0][5] is None


def _assert_api_cannot_read_secure_payload() -> None:
    with _connection("P3B_API_DSN") as connection:
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                    cursor.execute(
                        "SELECT payload_encrypted "
                        "FROM public.organization_registration_payloads_secure"
                    )
                    cursor.fetchall()
        except psycopg.Error as exc:
            assert exc.sqlstate == "42501", exc
        else:
            raise AssertionError("API runtime unexpectedly read secure registration payload")


def main() -> None:
    _seed_principals()
    key_version = _install_alpha_key()
    primary_payload = _envelope(
        registration_id=PRIMARY_REG,
        key_version=key_version,
    )

    created = _call_create(
        registration_id=PRIMARY_REG,
        key_version=key_version,
        payload=primary_payload,
    )
    assert created == (
        PRIMARY_REG,
        "PAN",
        MASK,
        "IN",
        "P",
        False,
        None,
    )
    _assert_primary_persisted(key_version, primary_payload)
    _assert_masked_read()
    _assert_api_cannot_read_secure_payload()

    duplicate_payload = _envelope(
        registration_id=DUPLICATE_REG,
        key_version=key_version,
        identifier="ZZZZZ9999Z",
    )
    _expect_sqlstate(
        "23505",
        registration_id=DUPLICATE_REG,
        key_version=key_version,
        payload=duplicate_payload,
    )
    _assert_registration_absent(DUPLICATE_REG)

    bad_header_payload = (
        struct.pack(">I", key_version + 1) + primary_payload[4:]
    )
    _expect_sqlstate(
        "22023",
        registration_id=BAD_HEADER_REG,
        key_version=key_version,
        payload=bad_header_payload,
        id_type="GST",
    )
    _assert_registration_absent(BAD_HEADER_REG)

    missing_key_version = key_version + 1000
    _expect_sqlstate(
        "23503",
        registration_id=MISSING_KEY_REG,
        key_version=missing_key_version,
        payload=_envelope(
            registration_id=MISSING_KEY_REG,
            key_version=missing_key_version,
        ),
        id_type="GST",
    )
    _assert_registration_absent(MISSING_KEY_REG)

    _expect_sqlstate(
        "22023",
        registration_id=LOWERCASE_REG,
        key_version=key_version,
        payload=_envelope(
            registration_id=LOWERCASE_REG,
            key_version=key_version,
        ),
        id_type="pan",
    )
    _assert_registration_absent(LOWERCASE_REG)

    _expect_sqlstate(
        "42501",
        registration_id=CROSS_TENANT_REG,
        key_version=key_version,
        payload=_envelope(
            registration_id=CROSS_TENANT_REG,
            key_version=key_version,
        ),
        org_id=ALPHA_ORG,
        user_id=BETA_OWNER,
        id_type="GST",
    )
    _assert_registration_absent(CROSS_TENANT_REG)

    _expect_sqlstate(
        "42501",
        registration_id=BRANCH_REG,
        key_version=key_version,
        payload=_envelope(
            registration_id=BRANCH_REG,
            key_version=key_version,
        ),
        gym_id=BRANCH_ID,
        id_type="GST",
    )
    _assert_registration_absent(BRANCH_REG)

    print("P3B registration create runtime boundary: PASS")


if __name__ == "__main__":
    main()
