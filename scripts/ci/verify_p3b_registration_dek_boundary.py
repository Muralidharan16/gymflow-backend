from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg


ALPHA_ORG = "11111111-1111-4111-8111-111111111111"
BETA_ORG = "22222222-2222-4222-8222-222222222222"
ALPHA_OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BETA_OWNER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ALPHA_KEY = b"kms-wrapped-alpha-registration-dek"
BETA_KEY_A = b"kms-wrapped-beta-registration-dek-a"
BETA_KEY_B = b"kms-wrapped-beta-registration-dek-b"


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
    org_id: str,
    user_id: str,
    principal_type: str = "owner",
    role: str = "owner",
    gym_id: str = "",
) -> None:
    for name, value in (
        ("app.current_org_id", org_id),
        ("app.current_user_id", user_id),
        ("app.current_principal_type", principal_type),
        ("app.current_role", role),
        ("app.current_gym_id", gym_id),
    ):
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
                      (%s::uuid, 'Alpha Gym', 'alpha-dek-gym', 'basic', TRUE),
                      (%s::uuid, 'Beta Gym', 'beta-dek-gym', 'basic', TRUE)
                    """,
                    (ALPHA_ORG, BETA_ORG),
                )
                cursor.execute(
                    """
                    INSERT INTO public.owners (
                        id, org_id, owner_name, email, hashed_password,
                        email_verified, onboarding_completed
                    ) VALUES
                      (%s::uuid, %s::uuid, 'Alpha Owner',
                       'alpha-dek-owner@example.test', 'not-a-real-password', FALSE, FALSE),
                      (%s::uuid, %s::uuid, 'Beta Owner',
                       'beta-dek-owner@example.test', 'not-a-real-password', FALSE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )


def _expect_sqlstate(
    expected: str,
    *,
    context: dict[str, str] | None,
    sql: str,
    params: tuple = (),
) -> None:
    with _connection("P3B_API_DSN") as connection:
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    if context is not None:
                        _set_context(cursor, **context)
                    cursor.execute(sql, params)
                    if cursor.description is not None:
                        cursor.fetchall()
        except psycopg.Error as exc:
            if exc.sqlstate != expected:
                raise AssertionError(
                    f"expected SQLSTATE {expected}, observed {exc.sqlstate}: {exc}"
                ) from exc
        else:
            raise AssertionError(f"query unexpectedly succeeded: {sql}")


def _prove_catalog_acl() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attribute_data.attname::text,
                       acl_data.privilege_type::text
                FROM pg_catalog.pg_attribute AS attribute_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE attribute_data.attrelid =
                      'public.encryption_key_registry'::regclass
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND grantee_role.rolname = 'app_security_owner'
                ORDER BY 1, 2
                """
            )
            assert set(cursor.fetchall()) == {
                ("tenant_id", "SELECT"),
                ("key_version", "SELECT"),
                ("encrypted_dek", "SELECT"),
                ("table_name", "SELECT"),
                ("key_status", "SELECT"),
                ("tenant_id", "INSERT"),
                ("table_name", "INSERT"),
                ("encrypted_dek", "INSERT"),
                ("key_status", "INSERT"),
            }

            cursor.execute(
                """
                SELECT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS sequence_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(sequence_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE sequence_data.oid =
                      'public.encryption_key_registry_key_version_seq'::regclass
                  AND grantee_role.rolname = 'app_security_owner'
                ORDER BY 1
                """
            )
            assert cursor.fetchall() == [("USAGE",)]

            cursor.execute(
                """
                SELECT ns.nspname::text,
                       procedure_data.proname::text,
                       owner_role.rolname::text,
                       procedure_data.prosecdef,
                       procedure_data.provolatile::text,
                       procedure_data.proconfig
                FROM pg_catalog.pg_proc AS procedure_data
                JOIN pg_catalog.pg_namespace AS ns
                  ON ns.oid = procedure_data.pronamespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = procedure_data.proowner
                WHERE ns.nspname = 'app_secure'
                  AND procedure_data.proname IN (
                      'current_registration_dek',
                      'install_registration_dek',
                      'lookup_registration_dek'
                  )
                ORDER BY procedure_data.proname
                """
            )
            rows = cursor.fetchall()
            assert [row[1] for row in rows] == [
                "current_registration_dek",
                "install_registration_dek",
                "lookup_registration_dek",
            ]
            for _, name, owner, security_definer, volatility, config in rows:
                assert owner == "app_security_owner"
                assert security_definer is True
                assert volatility == ("v" if name == "install_registration_dek" else "s")
                assert set(config or ()) == {
                    "search_path=pg_catalog",
                    "row_security=on",
                }


def _prove_alpha_install_and_lookup() -> int:
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute("SELECT * FROM app_secure.current_registration_dek()")
                assert cursor.fetchall() == []

                cursor.execute(
                    "SELECT * FROM app_secure.install_registration_dek(%s)",
                    (ALPHA_KEY,),
                )
                installed = cursor.fetchone()
                assert installed is not None
                key_version = int(installed[0])
                assert installed[1] == ALPHA_KEY

                cursor.execute("SELECT * FROM app_secure.current_registration_dek()")
                assert cursor.fetchone() == (key_version, ALPHA_KEY)

                cursor.execute(
                    "SELECT app_secure.lookup_registration_dek(%s)",
                    (key_version,),
                )
                assert cursor.fetchone()[0] == ALPHA_KEY

                cursor.execute(
                    "SELECT * FROM app_secure.install_registration_dek(%s)",
                    (b"should-not-replace-active-alpha-key",),
                )
                assert cursor.fetchone() == (key_version, ALPHA_KEY)
                return key_version


def _install_beta_concurrently(payload: bytes, barrier: threading.Barrier):
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=BETA_ORG, user_id=BETA_OWNER)
                barrier.wait(timeout=10)
                cursor.execute(
                    "SELECT * FROM app_secure.install_registration_dek(%s)",
                    (payload,),
                )
                return cursor.fetchone()


def _prove_concurrent_first_install_converges() -> tuple[int, bytes]:
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_install_beta_concurrently, BETA_KEY_A, barrier),
            executor.submit(_install_beta_concurrently, BETA_KEY_B, barrier),
        ]
        results = [future.result(timeout=20) for future in futures]

    assert results[0] == results[1]
    assert results[0] is not None
    key_version = int(results[0][0])
    winner = bytes(results[0][1])
    assert winner in {BETA_KEY_A, BETA_KEY_B}

    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT key_version, encrypted_dek, key_status
                FROM public.encryption_key_registry
                WHERE tenant_id = %s::uuid
                  AND table_name = 'organization_registrations'
                ORDER BY key_version
                """,
                (BETA_ORG,),
            )
            assert cursor.fetchall() == [(key_version, winner, "ACTIVE")]
    return key_version, winner


def _prove_tenant_and_context_isolation(alpha_key_version: int) -> None:
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=BETA_ORG, user_id=BETA_OWNER)
                cursor.execute(
                    "SELECT app_secure.lookup_registration_dek(%s)",
                    (alpha_key_version,),
                )
                assert cursor.fetchone()[0] is None

    owner_context = {"org_id": ALPHA_ORG, "user_id": ALPHA_OWNER}
    _expect_sqlstate(
        "22023",
        context=owner_context,
        sql="SELECT * FROM app_secure.install_registration_dek(%s)",
        params=(b"",),
    )
    _expect_sqlstate(
        "22023",
        context=owner_context,
        sql="SELECT app_secure.lookup_registration_dek(0)",
    )
    _expect_sqlstate(
        "42501",
        context=None,
        sql="SELECT * FROM app_secure.current_registration_dek()",
    )
    _expect_sqlstate(
        "42501",
        context={"org_id": ALPHA_ORG, "user_id": BETA_OWNER},
        sql="SELECT * FROM app_secure.current_registration_dek()",
    )
    _expect_sqlstate(
        "42501",
        context={
            "org_id": ALPHA_ORG,
            "user_id": ALPHA_OWNER,
            "role": "trainer",
        },
        sql="SELECT * FROM app_secure.current_registration_dek()",
    )
    _expect_sqlstate(
        "42501",
        context={
            "org_id": ALPHA_ORG,
            "user_id": ALPHA_OWNER,
            "gym_id": "33333333-3333-4333-8333-333333333333",
        },
        sql="SELECT * FROM app_secure.current_registration_dek()",
    )


def _prove_api_has_no_direct_registry_or_sequence_access() -> None:
    _expect_sqlstate(
        "42501",
        context=None,
        sql="SELECT key_version FROM public.encryption_key_registry LIMIT 1",
    )
    _expect_sqlstate(
        "42501",
        context=None,
        sql=(
            "SELECT nextval('public.encryption_key_registry_key_version_seq'::regclass)"
        ),
    )


def main() -> None:
    _seed_principals()
    _prove_catalog_acl()
    alpha_key_version = _prove_alpha_install_and_lookup()
    _prove_concurrent_first_install_converges()
    _prove_tenant_and_context_isolation(alpha_key_version)
    _prove_api_has_no_direct_registry_or_sequence_access()
    print("P3B registration DEK runtime boundary: PASS")


if __name__ == "__main__":
    main()
