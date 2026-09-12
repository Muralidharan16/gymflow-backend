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
REGISTRATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000011")
MISSING_TARGET_ID = uuid.UUID("10000000-0000-4000-8000-000000000012")
BRANCH_ID = uuid.UUID("30000000-0000-4000-8000-000000000011")
WRAPPED_DEK = b"kms-wrapped-registration-replace-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3b-replace"
PLAINTEXT_DEK = bytes(range(32))
OLD_MASK = "XXXXXX1111"
NEW_MASK = "XXXXXX4321"
SECOND_MASK = "XXXXXX9876"
LEGACY_CIPHERTEXT = "legacy-fernet-ciphertext"
VERIFIED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


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
    role: str = "owner",
    gym_id: uuid.UUID | None = None,
) -> None:
    for name, value in (
        ("app.current_org_id", str(org_id)),
        ("app.current_user_id", str(user_id)),
        ("app.current_principal_type", "owner"),
        ("app.current_role", role),
        ("app.current_gym_id", "" if gym_id is None else str(gym_id)),
    ):
        cursor.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)",
            (name, value),
        )


def _seed_principals_and_legacy_registration() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES
                      (%s, 'Alpha Replace Gym', 'alpha-replace-gym', 'basic', TRUE),
                      (%s, 'Beta Replace Gym', 'beta-replace-gym', 'basic', TRUE)
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s, %s, 'Alpha Owner', 'alpha-replace@example.test',
                       'not-a-real-password', FALSE, FALSE),
                      (%s, %s, 'Beta Owner', 'beta-replace@example.test',
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
                    (
                        REGISTRATION_ID,
                        ALPHA_ORG,
                        LEGACY_CIPHERTEXT,
                        OLD_MASK,
                        VERIFIED_AT,
                    ),
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
    identifier: str,
) -> bytes:
    aad = b"\x00".join(
        (
            b"doers:p3b:registration-envelope:v1",
            ALPHA_ORG.bytes,
            b"organization_registrations",
            registration_id.bytes,
            struct.pack(">I", key_version),
        )
    )
    nonce = bytes(range(12))
    return (
        struct.pack(">I", key_version)
        + nonce
        + AESGCM(PLAINTEXT_DEK).encrypt(nonce, identifier.encode(), aad)
    )


def _call_replace(
    *,
    registration_id: uuid.UUID,
    key_version: int,
    payload: bytes,
    mask: str,
    org_id: uuid.UUID = ALPHA_ORG,
    user_id: uuid.UUID = ALPHA_OWNER,
    gym_id: uuid.UUID | None = None,
):
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(
                    cursor,
                    org_id=org_id,
                    user_id=user_id,
                    gym_id=gym_id,
                )
                cursor.execute(
                    """
                    SELECT *
                    FROM app_secure.replace_organization_registration_envelope(
                        %s, 'PAN', %s, 'IN', 'P', %s, %s
                    )
                    """,
                    (registration_id, mask, payload, key_version),
                )
                return cursor.fetchone()


def _expect_sqlstate(expected: str, **kwargs) -> None:
    try:
        _call_replace(**kwargs)
    except psycopg.Error as exc:
        if exc.sqlstate != expected:
            raise AssertionError(
                f"expected SQLSTATE {expected}, observed {exc.sqlstate}: {exc}"
            ) from exc
    else:
        raise AssertionError(f"registration replacement unexpectedly succeeded: {kwargs}")


def _assert_legacy_unchanged() -> None:
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
                row = cursor.fetchone()
                assert row is not None
                assert row[:4] == (LEGACY_CIPHERTEXT, OLD_MASK, 0, True)
                assert row[4] == VERIFIED_AT
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.organization_registration_payloads_secure
                    WHERE registration_id = %s
                    """,
                    (REGISTRATION_ID,),
                )
                assert cursor.fetchone()[0] == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM public.p3b_registration_envelope_rows WHERE registration_id = %s",
                (REGISTRATION_ID,),
            )
            assert cursor.fetchone()[0] == 0


def _assert_envelope_state(*, key_version: int, payload: bytes, mask: str) -> None:
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
                assert cursor.fetchone() == (None, mask, 1, False, None)
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


def _assert_api_secure_storage_denied() -> None:
    with _connection("P3B_API_DSN") as connection:
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                    cursor.execute(
                        "SELECT payload_encrypted FROM public.organization_registration_payloads_secure"
                    )
                    cursor.fetchall()
        except psycopg.Error as exc:
            assert exc.sqlstate == "42501", exc
        else:
            raise AssertionError("API runtime unexpectedly read secure registration payload")


def main() -> None:
    _seed_principals_and_legacy_registration()
    key_version = _install_alpha_key()

    missing_version = key_version + 1000
    _expect_sqlstate(
        "23503",
        registration_id=REGISTRATION_ID,
        key_version=missing_version,
        payload=_envelope(
            registration_id=REGISTRATION_ID,
            key_version=missing_version,
            identifier="ABCDE4321F",
        ),
        mask=NEW_MASK,
    )
    _assert_legacy_unchanged()

    valid_payload = _envelope(
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        identifier="ABCDE4321F",
    )
    _expect_sqlstate(
        "42501",
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        payload=valid_payload,
        mask=NEW_MASK,
        user_id=BETA_OWNER,
    )
    _assert_legacy_unchanged()

    _expect_sqlstate(
        "42501",
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        payload=valid_payload,
        mask=NEW_MASK,
        gym_id=BRANCH_ID,
    )
    _assert_legacy_unchanged()

    missing_target_payload = _envelope(
        registration_id=MISSING_TARGET_ID,
        key_version=key_version,
        identifier="ABCDE4321F",
    )
    _expect_sqlstate(
        "P0002",
        registration_id=MISSING_TARGET_ID,
        key_version=key_version,
        payload=missing_target_payload,
        mask=NEW_MASK,
    )
    _assert_legacy_unchanged()

    replaced = _call_replace(
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        payload=valid_payload,
        mask=NEW_MASK,
    )
    assert replaced == (
        REGISTRATION_ID,
        "PAN",
        NEW_MASK,
        "IN",
        "P",
        False,
        None,
    )
    _assert_envelope_state(
        key_version=key_version,
        payload=valid_payload,
        mask=NEW_MASK,
    )

    second_payload = _envelope(
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        identifier="ABCDE9876F",
    )
    replaced_again = _call_replace(
        registration_id=REGISTRATION_ID,
        key_version=key_version,
        payload=second_payload,
        mask=SECOND_MASK,
    )
    assert replaced_again[0] == REGISTRATION_ID
    assert replaced_again[2] == SECOND_MASK
    assert replaced_again[5:] == (False, None)
    _assert_envelope_state(
        key_version=key_version,
        payload=second_payload,
        mask=SECOND_MASK,
    )
    _assert_api_secure_storage_denied()

    print("P3B registration replace runtime boundary: PASS")


if __name__ == "__main__":
    main()
