from __future__ import annotations

import os
import struct
from contextlib import contextmanager

import psycopg


ALPHA_ORG = "11111111-1111-4111-8111-111111111111"
BETA_ORG = "22222222-2222-4222-8222-222222222222"
ALPHA_OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BETA_OWNER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REGISTRATION_ID = "aaaaaaaa-2222-4aaa-8aaa-aaaaaaaaaaaa"
MASKED_ID = "XXXXXX1234"
WRAPPED_DEK = b"kms-wrapped-registration-read-dek"
WRAPPING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/p3b-read"


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


def _expect_42501(connection, *, context: dict[str, str] | None, sql: str) -> None:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                if context is not None:
                    _set_context(cursor, **context)
                cursor.execute(sql)
                cursor.fetchall()
    except psycopg.Error as exc:
        if exc.sqlstate != "42501":
            raise AssertionError(
                f"expected SQLSTATE 42501, observed {exc.sqlstate}: {exc}"
            ) from exc
    else:
        raise AssertionError(f"query unexpectedly succeeded: {sql}")


def _seed() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.organizations (id, name, slug, tier, is_active)
                    VALUES
                      (%s::uuid, 'Alpha Gym', 'alpha-gym', 'basic', TRUE),
                      (%s::uuid, 'Beta Gym', 'beta-gym', 'basic', TRUE)
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
                       'alpha-owner@example.test', 'not-a-real-password', FALSE, FALSE),
                      (%s::uuid, %s::uuid, 'Beta Owner',
                       'beta-owner@example.test', 'not-a-real-password', FALSE, FALSE)
                    """,
                    (ALPHA_OWNER, ALPHA_ORG, BETA_OWNER, BETA_ORG),
                )

    # Final P3B forbids legacy metadata-only rows. Seed the read proof through
    # the same principal-bound DEK + atomic create capabilities used by normal
    # application traffic.
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    "SELECT * FROM app_secure.install_registration_dek(%s, %s)",
                    (WRAPPED_DEK, WRAPPING_KEY_ID),
                )
                key_row = cursor.fetchone()
                assert key_row is not None
                key_version = int(key_row[0])
                payload = struct.pack(">I", key_version) + b"p3b-read-envelope-payload-padding"
                cursor.execute(
                    """
                    SELECT *
                    FROM app_secure.create_organization_registration_envelope(
                        %s::uuid, 'PAN', %s, 'IN', 'P', %s, %s
                    )
                    """,
                    (REGISTRATION_ID, MASKED_ID, payload, key_version),
                )
                created = cursor.fetchone()
                assert created is not None
                assert str(created[0]) == REGISTRATION_ID
                assert created[1:5] == ("PAN", MASKED_ID, "IN", "P")
                assert created[5] is False
                assert created[6] is None


def _prove_catalog_contract() -> None:
    with _connection("P3B_MIGRATION_DSN") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_catalog.pg_get_userbyid(c.relowner)::text,
                       c.relrowsecurity,
                       c.relforcerowsecurity
                FROM pg_catalog.pg_class AS c
                WHERE c.oid = 'public.organization_registrations'::regclass
                """
            )
            owner, rls_enabled, rls_forced = cursor.fetchone()
            assert owner == "migration_owner"
            assert rls_enabled is True
            assert rls_forced is True

            cursor.execute(
                """
                SELECT pg_catalog.has_column_privilege(
                           'app_security_owner',
                           'public.organization_registrations',
                           'id_number_masked',
                           'SELECT'
                       ),
                       pg_catalog.has_column_privilege(
                           'app_security_owner',
                           'public.organization_registrations',
                           'id_number_encrypted',
                           'SELECT'
                       )
                """
            )
            masked_allowed, encrypted_allowed = cursor.fetchone()
            assert masked_allowed is True
            assert encrypted_allowed is False

            cursor.execute(
                """
                SELECT p.proname::text,
                       owner_role.rolname::text,
                       p.prosecdef,
                       p.provolatile::text,
                       p.proconfig,
                       EXISTS (
                           SELECT 1
                           FROM pg_catalog.aclexplode(
                               COALESCE(
                                   p.proacl,
                                   pg_catalog.acldefault('f', p.proowner)
                               )
                           ) AS acl_data
                           JOIN pg_catalog.pg_roles AS grantee_role
                             ON grantee_role.oid = acl_data.grantee
                           WHERE grantee_role.rolname = 'app_runtime'
                             AND acl_data.privilege_type = 'EXECUTE'
                       ) AS runtime_execute,
                       EXISTS (
                           SELECT 1
                           FROM pg_catalog.aclexplode(
                               COALESCE(
                                   p.proacl,
                                   pg_catalog.acldefault('f', p.proowner)
                               )
                           ) AS acl_data
                           WHERE acl_data.grantee = 0
                             AND acl_data.privilege_type = 'EXECUTE'
                       ) AS public_execute
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS ns ON ns.oid = p.pronamespace
                JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = p.proowner
                WHERE ns.nspname = 'app_secure'
                  AND p.proname IN (
                      'current_organization_registrations',
                      'current_organization_has_registration'
                  )
                ORDER BY p.proname
                """
            )
            rows = cursor.fetchall()
            assert [row[0] for row in rows] == [
                "current_organization_has_registration",
                "current_organization_registrations",
            ]
            for _, owner_name, security_definer, volatility, config, api_exec, public_exec in rows:
                assert owner_name == "app_security_owner"
                assert security_definer is True
                assert volatility == "s"
                assert set(config or ()) == {
                    "search_path=pg_catalog",
                    "row_security=on",
                }
                assert api_exec is True
                assert public_exec is False


def _prove_valid_and_isolated_reads() -> None:
    with _connection("P3B_API_DSN") as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=ALPHA_ORG, user_id=ALPHA_OWNER)
                cursor.execute(
                    """
                    SELECT id, id_type, id_number_masked, country_code,
                           is_verified, verified_at
                    FROM app_secure.current_organization_registrations()
                    """
                )
                rows = cursor.fetchall()
                assert len(rows) == 1
                row = rows[0]
                assert str(row[0]) == REGISTRATION_ID
                assert row[1] == "PAN"
                assert row[2] == MASKED_ID
                assert row[3] == "IN"
                assert row[4] is False
                assert row[5] is None

                cursor.execute(
                    "SELECT app_secure.current_organization_has_registration()"
                )
                assert cursor.fetchone()[0] is True

        with connection.transaction():
            with connection.cursor() as cursor:
                _set_context(cursor, org_id=BETA_ORG, user_id=BETA_OWNER)
                cursor.execute(
                    "SELECT count(*) FROM app_secure.current_organization_registrations()"
                )
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    "SELECT app_secure.current_organization_has_registration()"
                )
                assert cursor.fetchone()[0] is False


def _prove_fail_closed_contexts() -> None:
    with _connection("P3B_API_DSN") as connection:
        _expect_42501(
            connection,
            context=None,
            sql="SELECT * FROM app_secure.current_organization_registrations()",
        )
        _expect_42501(
            connection,
            context={"org_id": "not-a-uuid", "user_id": ALPHA_OWNER},
            sql="SELECT * FROM app_secure.current_organization_registrations()",
        )
        _expect_42501(
            connection,
            context={"org_id": ALPHA_ORG, "user_id": BETA_OWNER},
            sql="SELECT * FROM app_secure.current_organization_registrations()",
        )
        _expect_42501(
            connection,
            context={"org_id": ALPHA_ORG, "user_id": ALPHA_OWNER, "role": "trainer"},
            sql="SELECT * FROM app_secure.current_organization_registrations()",
        )
        _expect_42501(
            connection,
            context={
                "org_id": ALPHA_ORG,
                "user_id": ALPHA_OWNER,
                "gym_id": "33333333-3333-4333-8333-333333333333",
            },
            sql="SELECT app_secure.current_organization_has_registration()",
        )


def main() -> None:
    _seed()
    _prove_catalog_contract()
    _prove_valid_and_isolated_reads()
    _prove_fail_closed_contexts()
    print("P3B registration read runtime boundary: PASS")


if __name__ == "__main__":
    main()
