from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parents[2]
D07 = "d07d8e9f0a24"
C97 = "c97d8e9f0a23"
ORG_ID = "11111111-1111-4111-8111-111111111111"
LEGACY_REG_ID = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
ENVELOPE_REG_ID = "aaaaaaaa-2222-4aaa-8aaa-aaaaaaaaaaaa"
LEGACY_CIPHERTEXT = "legacy-fernet-ciphertext"
LEGACY_MASK = "XXXXXX1234"
ENVELOPE_MASK = "XXXXXXXXX9999"


@contextmanager
def _connection(env_name: str):
    dsn = os.environ.get(env_name, "").strip()
    if not dsn:
        raise RuntimeError(f"{env_name} is required")
    with psycopg.connect(dsn) as connection:
        yield connection


def _alembic(command: str, revision: str, *, expect_success: bool = True):
    result = subprocess.run(
        [sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if expect_success and result.returncode != 0:
        raise AssertionError(
            f"alembic {command} {revision} failed unexpectedly:\n{combined}"
        )
    if not expect_success and result.returncode == 0:
        raise AssertionError(
            f"alembic {command} {revision} unexpectedly succeeded:\n{combined}"
        )
    return result, combined


def _set_org(cursor) -> None:
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_org_id', %s, true)",
        (ORG_ID,),
    )


def _version(connection) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version_num::text FROM public.alembic_version")
        return str(cursor.fetchone()[0])


def _relation_exists(connection, relation: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (relation,))
        return bool(cursor.fetchone()[0])


def _column_exists(connection, relation: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_attribute AS attribute_data
                WHERE attribute_data.attrelid = pg_catalog.to_regclass(%s)
                  AND attribute_data.attname = %s
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
            )
            """,
            (relation, column),
        )
        return bool(cursor.fetchone()[0])


def _seed_legacy_row() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_org(cursor)
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES (%s::uuid, 'P3B Migration Gym', 'p3b-migration-gym', 'basic', TRUE)
                    """,
                    (ORG_ID,),
                )
                cursor.execute(
                    """
                    INSERT INTO public.organization_registrations (
                        id, org_id, id_type, id_number_encrypted,
                        id_number_masked, country_code, entity_type,
                        crypto_version, is_verified
                    ) VALUES (
                        %s::uuid, %s::uuid, 'PAN', %s, %s, 'IN', 'P', 0, FALSE
                    )
                    """,
                    (LEGACY_REG_ID, ORG_ID, LEGACY_CIPHERTEXT, LEGACY_MASK),
                )


def _assert_legacy_row(*, expect_crypto_column: bool) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_org(cursor)
                if expect_crypto_column:
                    cursor.execute(
                        """
                        SELECT id_number_encrypted, id_number_masked, crypto_version
                        FROM public.organization_registrations
                        WHERE id = %s::uuid AND org_id = %s::uuid
                        """,
                        (LEGACY_REG_ID, ORG_ID),
                    )
                    row = cursor.fetchone()
                    assert row == (LEGACY_CIPHERTEXT, LEGACY_MASK, 0)
                else:
                    cursor.execute(
                        """
                        SELECT id_number_encrypted, id_number_masked
                        FROM public.organization_registrations
                        WHERE id = %s::uuid AND org_id = %s::uuid
                        """,
                        (LEGACY_REG_ID, ORG_ID),
                    )
                    row = cursor.fetchone()
                    assert row == (LEGACY_CIPHERTEXT, LEGACY_MASK)


def _assert_storage_shape(*, present: bool) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        assert _relation_exists(
            connection,
            "public.organization_registration_payloads_secure",
        ) is present
        assert _relation_exists(
            connection,
            "public.p3b_registration_envelope_rows",
        ) is present
        assert _column_exists(
            connection,
            "public.organization_registrations",
            "crypto_version",
        ) is present


def _assert_runtime_has_no_direct_storage_acl() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT relation_data.relname::text,
                       grantee_role.rolname::text,
                       acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname IN (
                      'organization_registration_payloads_secure',
                      'p3b_registration_envelope_rows'
                  )
                  AND grantee_role.rolname IN ('app_runtime', 'auth_runtime')
                ORDER BY 1, 2, 3
                """
            )
            assert cursor.fetchall() == []


def _expect_api_42501(sql: str, params: tuple = ()) -> None:
    with _connection("P3B_API_DSN") as connection:
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    if cursor.description is not None:
                        cursor.fetchall()
        except psycopg.Error as exc:
            if exc.sqlstate != "42501":
                raise AssertionError(
                    f"expected SQLSTATE 42501, observed {exc.sqlstate}: {exc}"
                ) from exc
        else:
            raise AssertionError(f"API runtime unexpectedly executed direct storage SQL: {sql}")


def _seed_envelope_row() -> int:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_org(cursor)
                cursor.execute(
                    """
                    INSERT INTO public.encryption_key_registry (
                        tenant_id, table_name, encrypted_dek, key_status
                    ) VALUES (%s::uuid, 'organization_registrations', %s, 'ACTIVE')
                    RETURNING key_version
                    """,
                    (ORG_ID, b"kms-wrapped-test-dek"),
                )
                key_version = int(cursor.fetchone()[0])

                cursor.execute(
                    """
                    INSERT INTO public.organization_registrations (
                        id, org_id, id_type, id_number_encrypted,
                        id_number_masked, country_code, entity_type,
                        crypto_version, is_verified
                    ) VALUES (
                        %s::uuid, %s::uuid, 'GST', NULL, %s, 'IN', NULL, 1, FALSE
                    )
                    """,
                    (ENVELOPE_REG_ID, ORG_ID, ENVELOPE_MASK),
                )

                dek = bytes(range(32))
                nonce = bytes(range(12))
                aad = f"{ORG_ID}:organization_registrations:{ENVELOPE_REG_ID}".encode()
                encrypted = AESGCM(dek).encrypt(nonce, b"GSTIN-TEST-9999", aad)
                envelope = key_version.to_bytes(4, "big") + nonce + encrypted

                cursor.execute(
                    """
                    INSERT INTO public.organization_registration_payloads_secure (
                        registration_id, tenant_id, payload_encrypted,
                        key_version, schema_version
                    ) VALUES (%s::uuid, %s::uuid, %s, %s, 1)
                    """,
                    (ENVELOPE_REG_ID, ORG_ID, envelope, key_version),
                )

                cursor.execute(
                    """
                    SELECT registration_id::text
                    FROM public.p3b_registration_envelope_rows
                    WHERE registration_id = %s::uuid
                    """,
                    (ENVELOPE_REG_ID,),
                )
                assert cursor.fetchone() == (ENVELOPE_REG_ID,)
                return key_version


def _assert_envelope_survived_failed_downgrade(key_version: int) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        assert _version(connection) == D07
        assert _relation_exists(
            connection,
            "public.organization_registration_payloads_secure",
        )
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_org(cursor)
                cursor.execute(
                    """
                    SELECT registration_id::text, tenant_id::text, key_version
                    FROM public.organization_registration_payloads_secure
                    WHERE registration_id = %s::uuid
                    """,
                    (ENVELOPE_REG_ID,),
                )
                assert cursor.fetchone() == (ENVELOPE_REG_ID, ORG_ID, key_version)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT registration_id::text FROM public.p3b_registration_envelope_rows"
            )
            assert cursor.fetchall() == [(ENVELOPE_REG_ID,)]


def _cleanup_envelope_row(key_version: int) -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_org(cursor)
                cursor.execute(
                    "DELETE FROM public.organization_registration_payloads_secure "
                    "WHERE registration_id = %s::uuid",
                    (ENVELOPE_REG_ID,),
                )
                assert cursor.rowcount == 1
                cursor.execute(
                    "DELETE FROM public.organization_registrations "
                    "WHERE id = %s::uuid AND org_id = %s::uuid",
                    (ENVELOPE_REG_ID, ORG_ID),
                )
                assert cursor.rowcount == 1
                cursor.execute(
                    "DELETE FROM public.encryption_key_registry WHERE key_version = %s",
                    (key_version,),
                )
                assert cursor.rowcount == 1

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM public.p3b_registration_envelope_rows")
            assert cursor.fetchone()[0] == 0


def main() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        assert _version(connection) == D07

    _seed_legacy_row()
    _assert_legacy_row(expect_crypto_column=True)

    # A predecessor-compatible legacy row must survive d07 -> c97 -> d07.
    _alembic("downgrade", C97)
    _assert_storage_shape(present=False)
    _assert_legacy_row(expect_crypto_column=False)
    _alembic("upgrade", D07)
    _assert_storage_shape(present=True)
    _assert_legacy_row(expect_crypto_column=True)

    _assert_runtime_has_no_direct_storage_acl()
    _expect_api_42501(
        "SELECT count(*) FROM public.organization_registration_payloads_secure"
    )
    _expect_api_42501("SELECT count(*) FROM public.p3b_registration_envelope_rows")
    _expect_api_42501(
        "INSERT INTO public.p3b_registration_envelope_rows (registration_id) "
        "VALUES (%s::uuid)",
        ("33333333-3333-4333-8333-333333333333",),
    )

    key_version = _seed_envelope_row()
    _, failed_output = _alembic("downgrade", C97, expect_success=False)
    assert "P3B downgrade would discard KMS-backed registration envelope data" in failed_output
    _assert_envelope_survived_failed_downgrade(key_version)

    _cleanup_envelope_row(key_version)
    _alembic("downgrade", C97)
    _assert_storage_shape(present=False)
    _assert_legacy_row(expect_crypto_column=False)
    _alembic("upgrade", D07)
    _assert_storage_shape(present=True)
    _assert_legacy_row(expect_crypto_column=True)

    print("P3B registration storage migration adversarial gate: PASS")


if __name__ == "__main__":
    main()
