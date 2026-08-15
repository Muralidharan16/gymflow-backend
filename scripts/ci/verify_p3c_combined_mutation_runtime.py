from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import contextmanager

import psycopg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.aws_kms import AWSKMSUnavailableError, EncryptedDataKey
from app.core.database import SessionContextInitializer
from app.repositories.organization_profile import ProfileAuthorizationError
import app.services.organization_profile_mutation_service as mutation_service
from app.services.organization_profile_mutation_service import RegistrationMutationPlan
import app.services.registration_key_service as registration_key_service


ALPHA_ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
BETA_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
ALPHA_OWNER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BETA_OWNER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PLAINTEXT_DEK = bytes(range(32))
WRAPPED_DEK = b"p3c-deterministic-kms-wrapped-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3c-runtime"


@contextmanager
def _migration_connection():
    dsn = os.environ.get("P3C_MIGRATION_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3C_MIGRATION_DSN is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _api_async_dsn() -> str:
    dsn = os.environ.get("P3C_API_ASYNC_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3C_API_ASYNC_DSN is required")
    return dsn


def _set_context(cursor, *, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    values = (
        ("app.current_org_id", str(org_id)),
        ("app.current_user_id", str(user_id)),
        ("app.current_user", str(user_id)),
        ("app.current_principal_type", "owner"),
        ("app.current_role", "owner"),
        ("app.current_gym_id", ""),
    )
    for name, value in values:
        cursor.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)",
            (name, value),
        )


def _seed_principals() -> None:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES
                      (%s, 'Alpha P3C Gym', 'alpha-p3c-gym', 'basic', TRUE),
                      (%s, 'Beta P3C Gym', 'beta-p3c-gym', 'basic', TRUE)
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s, %s, 'Alpha P3C Owner',
                       'alpha-p3c-owner@example.test', 'not-a-real-password', FALSE, FALSE),
                      (%s, %s, 'Beta P3C Owner',
                       'beta-p3c-owner@example.test', 'not-a-real-password', FALSE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )


def _snapshot() -> tuple[str, tuple | None, tuple | None]:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT name FROM public.organizations WHERE id = %s",
                    (ALPHA_ORG,),
                )
                org = cursor.fetchone()
                assert org is not None
                cursor.execute(
                    """
                    SELECT id::text, id_type, id_number_masked, country_code,
                           entity_type, crypto_version, is_verified, verified_at
                    FROM public.organization_registrations
                    WHERE org_id = %s AND id_type = 'PAN' AND country_code = 'IN'
                    """,
                    (ALPHA_ORG,),
                )
                registration = cursor.fetchone()
                payload = None
                if registration is not None:
                    cursor.execute(
                        """
                        SELECT registration_id::text, payload_encrypted, key_version,
                               key_scope, schema_version
                        FROM public.organization_registration_payloads_secure
                        WHERE registration_id = %s
                        """,
                        (uuid.UUID(registration[0]),),
                    )
                    payload = cursor.fetchone()
                return str(org[0]), registration, payload


def _mark_pan_verified() -> None:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    UPDATE public.organization_registrations
                    SET is_verified = TRUE,
                        verified_at = pg_catalog.clock_timestamp()
                    WHERE org_id = %s AND id_type = 'PAN' AND country_code = 'IN'
                    """,
                    (ALPHA_ORG,),
                )
                assert cursor.rowcount == 1


class _DeterministicKMS:
    fail_decrypt = False
    generate_calls = 0
    decrypt_calls = 0

    async def generate_encrypted_data_key(self) -> EncryptedDataKey:
        type(self).generate_calls += 1
        return EncryptedDataKey(ciphertext=WRAPPED_DEK, key_id=WRAPPING_KEY_ID)

    async def decrypt_dek(self, encrypted_dek: bytes, wrapping_key_id: str) -> bytes:
        type(self).decrypt_calls += 1
        if type(self).fail_decrypt:
            raise AWSKMSUnavailableError("injected P3C KMS failure")
        assert encrypted_dek == WRAPPED_DEK
        assert wrapping_key_id == WRAPPING_KEY_ID
        return PLAINTEXT_DEK


def _provider(_tenant_id: str) -> _DeterministicKMS:
    return _DeterministicKMS()


def _pan(identifier: str) -> RegistrationMutationPlan:
    return RegistrationMutationPlan(
        id_type="PAN",
        normalized_identifier=identifier,
        masked_identifier="X" * (len(identifier) - 4) + identifier[-4:],
        country_code="IN",
        entity_type=identifier[3],
    )


async def _initialize_owner(session, *, org_id=ALPHA_ORG, user_id=ALPHA_OWNER) -> None:
    await SessionContextInitializer.initialize(
        session,
        user_id=str(user_id),
        principal_type="owner",
        org_id=str(org_id),
        gym_id=None,
        trace_id="p3c-runtime",
        role="owner",
    )


async def _run() -> None:
    registration_key_service.registration_kms_provider = _provider

    engine = create_async_engine(_api_async_dsn(), pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Profile-only: no KMS or registration mutation is touched.
        _DeterministicKMS.generate_calls = 0
        _DeterministicKMS.decrypt_calls = 0
        async with sessions() as session:
            await _initialize_owner(session)
            result = await mutation_service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "Alpha Profile Only"},
                registration_updates=(),
            )
            assert result.profile["name"] == "Alpha Profile Only"
        assert _DeterministicKMS.generate_calls == 0
        assert _DeterministicKMS.decrypt_calls == 0
        name, registration, payload = _snapshot()
        assert name == "Alpha Profile Only"
        assert registration is None
        assert payload is None

        # Registration-only: profile remains unchanged while the real P3B DEK,
        # envelope crypto and create capability commit in the same service UoW.
        async with sessions() as session:
            await _initialize_owner(session)
            await mutation_service.mutate_organization_profile_atomically(
                session,
                profile_patch={},
                registration_updates=(_pan("ABCDE1234F"),),
            )
        name, registration, payload = _snapshot()
        assert name == "Alpha Profile Only"
        assert registration is not None
        assert registration[1:] == (
            "PAN",
            "XXXXXX234F",
            "IN",
            "D",
            1,
            False,
            None,
        )
        assert payload is not None
        assert payload[0] == registration[0]
        assert payload[3:] == ("organization_registrations", 1)
        assert _DeterministicKMS.generate_calls == 1
        assert _DeterministicKMS.decrypt_calls >= 1

        # Combined replace: profile and registration commit together and a new
        # identifier cannot inherit verification from the old identifier.
        _mark_pan_verified()
        async with sessions() as session:
            await _initialize_owner(session)
            await mutation_service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": "Alpha Combined"},
                registration_updates=(_pan("FGHIJ5678K"),),
            )
        name, registration, payload = _snapshot()
        assert name == "Alpha Combined"
        assert registration is not None
        assert registration[2] == "XXXXXX678K"
        assert registration[6] is False
        assert registration[7] is None
        assert payload is not None
        before_kms_failure = (name, registration, payload)

        # Real P3B KMS-path failure occurs after the P3A profile UPDATE.  The
        # explicit P3C transaction must restore profile, metadata and payload.
        _DeterministicKMS.fail_decrypt = True
        async with sessions() as session:
            await _initialize_owner(session)
            try:
                await mutation_service.mutate_organization_profile_atomically(
                    session,
                    profile_patch={"name": "MUST ROLLBACK KMS"},
                    registration_updates=(_pan("KLMNO9012P"),),
                )
            except AWSKMSUnavailableError as exc:
                assert "injected P3C KMS failure" in str(exc)
            else:
                raise AssertionError("injected KMS failure unexpectedly committed")
            assert not session.in_transaction()
        _DeterministicKMS.fail_decrypt = False
        assert _snapshot() == before_kms_failure

        # Cancellation is a BaseException path.  It must roll back the real P3A
        # update, leave the P3B row/payload untouched, and keep the same session
        # reusable with request context reapplied on the next transaction.
        original_replace = mutation_service.replace_secure_organization_registration

        async def _cancel_replace(*args, **kwargs):
            raise asyncio.CancelledError

        mutation_service.replace_secure_organization_registration = _cancel_replace
        try:
            async with sessions() as session:
                await _initialize_owner(session)
                try:
                    await mutation_service.mutate_organization_profile_atomically(
                        session,
                        profile_patch={"name": "MUST ROLLBACK CANCEL"},
                        registration_updates=(_pan("PQRST3456U"),),
                    )
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("injected cancellation unexpectedly committed")
                assert not session.in_transaction()

                # Same session, new transaction, same verified principal context.
                recovery = await mutation_service.mutate_organization_profile_atomically(
                    session,
                    profile_patch={"name": "Alpha After Cancel"},
                    registration_updates=(),
                )
                assert recovery.profile["name"] == "Alpha After Cancel"
        finally:
            mutation_service.replace_secure_organization_registration = original_replace
        name, registration, payload = _snapshot()
        assert name == "Alpha After Cancel"
        assert registration == before_kms_failure[1]
        assert payload == before_kms_failure[2]

        # An unrelated owner cannot use an Alpha tenant context.  P3A rejects
        # the first business mutation and the transaction leaves all state intact.
        before_auth_failure = _snapshot()
        async with sessions() as session:
            await _initialize_owner(session, org_id=ALPHA_ORG, user_id=BETA_OWNER)
            try:
                await mutation_service.mutate_organization_profile_atomically(
                    session,
                    profile_patch={"name": "MUST ROLLBACK AUTH"},
                    registration_updates=(_pan("UVWXY7890Z"),),
                )
            except ProfileAuthorizationError:
                pass
            else:
                raise AssertionError("cross-tenant principal unexpectedly mutated Alpha")
            assert not session.in_transaction()
        assert _snapshot() == before_auth_failure

        # Two concurrent combined mutations must serialize through the P3A root
        # row rather than commit a mixed profile/registration pair.
        pair_a = ("Concurrency Pair A", _pan("AAAAA1111A"))
        pair_b = ("Concurrency Pair B", _pan("BBBBB2222B"))

        async def _combined(name_value: str, plan: RegistrationMutationPlan) -> None:
            async with sessions() as session:
                await _initialize_owner(session)
                await mutation_service.mutate_organization_profile_atomically(
                    session,
                    profile_patch={"name": name_value},
                    registration_updates=(plan,),
                )

        await asyncio.gather(
            _combined(*pair_a),
            _combined(*pair_b),
        )
        name, registration, payload = _snapshot()
        assert registration is not None
        final_pair = (name, registration[2])
        expected_pairs = {
            (pair_a[0], pair_a[1].masked_identifier),
            (pair_b[0], pair_b[1].masked_identifier),
        }
        assert final_pair in expected_pairs, (final_pair, expected_pairs)
        assert payload is not None

    finally:
        await engine.dispose()


def main() -> None:
    _seed_principals()
    asyncio.run(_run())
    print("P3C combined profile + registration runtime atomicity: PASS")


if __name__ == "__main__":
    main()
