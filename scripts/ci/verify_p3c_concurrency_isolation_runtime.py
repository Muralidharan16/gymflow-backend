from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import contextmanager

import psycopg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.aws_kms import EncryptedDataKey
from app.core.database import SessionContextInitializer
from app.repositories.organization_registration_mutations import RegistrationConflictError
import app.services.organization_profile_mutation_service as mutation_service
from app.services.organization_profile_mutation_service import RegistrationMutationPlan
import app.services.registration_key_service as registration_key_service


ORG_ID = uuid.UUID("53333333-3333-4333-8333-333333333333")
OWNER_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
PLAINTEXT_DEK = bytes(range(32))
WRAPPED_DEK = b"p3c-concurrency-wrapped-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3c-concurrency"


@contextmanager
def _migration_connection():
    dsn = os.environ.get("P3C_CONCURRENCY_MIGRATION_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3C_CONCURRENCY_MIGRATION_DSN is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _api_dsn() -> str:
    dsn = os.environ.get("P3C_CONCURRENCY_API_DSN", "").strip()
    if not dsn:
        raise RuntimeError("P3C_CONCURRENCY_API_DSN is required")
    return dsn


def _set_context(cursor) -> None:
    for name, value in (
        ("app.current_org_id", str(ORG_ID)),
        ("app.current_user_id", str(OWNER_ID)),
        ("app.current_user", str(OWNER_ID)),
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
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES (%s, 'P3C Concurrency Before', 'p3c-concurrency', 'basic', TRUE)
                    """,
                    (ORG_ID,),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES (
                        %s, %s, 'P3C Concurrency Owner',
                        'p3c-concurrency@example.test', 'not-a-real-password', TRUE, FALSE
                    )
                    """,
                    (OWNER_ID, ORG_ID),
                )


def _snapshot() -> tuple[str, list[tuple], int, int]:
    with _migration_connection() as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor)
                cursor.execute("SELECT name FROM public.organizations WHERE id = %s", (ORG_ID,))
                org = cursor.fetchone()
                assert org is not None
                cursor.execute(
                    """
                    SELECT id::text, id_type, id_number_masked, country_code,
                           crypto_version, is_verified, verified_at
                    FROM public.organization_registrations
                    WHERE org_id = %s
                    ORDER BY id_type, country_code
                    """,
                    (ORG_ID,),
                )
                registrations = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.organization_registration_payloads_secure
                    WHERE tenant_id = %s
                    """,
                    (ORG_ID,),
                )
                payload_count = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.encryption_key_registry
                    WHERE tenant_id = %s
                      AND table_name = 'organization_registrations'
                      AND key_status = 'ACTIVE'
                    """,
                    (ORG_ID,),
                )
                active_key_count = int(cursor.fetchone()[0])
                return str(org[0]), registrations, payload_count, active_key_count


class _RaceKMS:
    generate_calls = 0
    decrypt_calls = 0

    async def generate_encrypted_data_key(self) -> EncryptedDataKey:
        type(self).generate_calls += 1
        return EncryptedDataKey(ciphertext=WRAPPED_DEK, key_id=WRAPPING_KEY_ID)

    async def decrypt_dek(self, encrypted_dek: bytes, wrapping_key_id: str) -> bytes:
        type(self).decrypt_calls += 1
        assert encrypted_dek == WRAPPED_DEK
        assert wrapping_key_id == WRAPPING_KEY_ID
        return PLAINTEXT_DEK


def _provider(_tenant_id: str) -> _RaceKMS:
    return _RaceKMS()


def _pan(identifier: str) -> RegistrationMutationPlan:
    return RegistrationMutationPlan(
        id_type="PAN",
        normalized_identifier=identifier,
        masked_identifier="X" * (len(identifier) - 4) + identifier[-4:],
        country_code="IN",
        entity_type=identifier[3],
    )


async def _initialize(session) -> None:
    await SessionContextInitializer.initialize(
        session,
        user_id=str(OWNER_ID),
        principal_type="owner",
        org_id=str(ORG_ID),
        gym_id=None,
        trace_id="p3c-concurrency",
        role="owner",
    )


async def _run() -> None:
    registration_key_service.registration_kms_provider = _provider
    engine = create_async_engine(_api_dsn(), pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    original_list = mutation_service.list_current_organization_registrations
    barrier = asyncio.Barrier(2)
    initial_reads = 0
    counter_lock = asyncio.Lock()

    async def _barrier_list(session):
        nonlocal initial_reads
        rows = await original_list(session)
        should_wait = False
        async with counter_lock:
            if initial_reads < 2:
                initial_reads += 1
                should_wait = True
        if should_wait:
            assert rows == [], rows
            await barrier.wait()
        return rows

    mutation_service.list_current_organization_registrations = _barrier_list
    try:
        requests = (
            ("P3C Race Winner A", _pan("ABCDE1111F")),
            ("P3C Race Winner B", _pan("FGHIJ2222K")),
        )

        async def _attempt(name: str, plan: RegistrationMutationPlan):
            async with sessions() as session:
                await _initialize(session)
                try:
                    result = await mutation_service.mutate_organization_profile_atomically(
                        session,
                        profile_patch={"name": name},
                        registration_updates=(plan,),
                    )
                    return ("success", name, plan.masked_identifier, result.profile["name"])
                except RegistrationConflictError:
                    assert not session.in_transaction()
                    return ("conflict", name, plan.masked_identifier, None)

        outcomes = await asyncio.gather(*(_attempt(*request) for request in requests))
        success = [outcome for outcome in outcomes if outcome[0] == "success"]
        conflicts = [outcome for outcome in outcomes if outcome[0] == "conflict"]
        assert len(success) == 1, outcomes
        assert len(conflicts) == 1, outcomes

        final_name, registrations, payload_count, active_key_count = _snapshot()
        assert len(registrations) == 1, registrations
        registration = registrations[0]
        assert registration[1] == "PAN"
        assert registration[3] == "IN"
        assert registration[4] == 1
        assert registration[5] is False
        assert registration[6] is None
        assert payload_count == 1
        assert active_key_count == 1

        winning = success[0]
        losing = conflicts[0]
        assert final_name == winning[1], (final_name, outcomes)
        assert registration[2] == winning[2], (registration, outcomes)
        assert final_name != losing[1]
        assert registration[2] != losing[2]

        # Retry the losing complete request after the race. It now takes the
        # certified replace path, and must commit as one complete pair without
        # duplicating metadata/payload/key rows.
        async with sessions() as session:
            await _initialize(session)
            retry = await mutation_service.mutate_organization_profile_atomically(
                session,
                profile_patch={"name": losing[1]},
                registration_updates=(
                    RegistrationMutationPlan(
                        id_type="PAN",
                        normalized_identifier=(
                            "ABCDE1111F" if losing[1].endswith("A") else "FGHIJ2222K"
                        ),
                        masked_identifier=losing[2],
                        country_code="IN",
                        entity_type=("D" if losing[1].endswith("A") else "I"),
                    ),
                ),
            )
            assert retry.profile["name"] == losing[1]

        final_name, registrations, payload_count, active_key_count = _snapshot()
        assert final_name == losing[1]
        assert len(registrations) == 1
        assert registrations[0][2] == losing[2]
        assert payload_count == 1
        assert active_key_count == 1

    finally:
        mutation_service.list_current_organization_registrations = original_list
        await engine.dispose()

    print("P3C registration creation race and isolation: PASS")


def main() -> None:
    _seed()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
