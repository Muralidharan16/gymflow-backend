from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[1]

MIGRATION_URL = os.environ.get(
    "PAY24B_REFUND_MIGRATION_DATABASE_URL"
)
ADMIN_URL = os.environ.get("PAY24B_REFUND_ADMIN_DATABASE_URL")
REFUND_URL = os.environ.get("PAY24B_REFUND_RUNTIME_DATABASE_URL")
PAYMENT_URL = os.environ.get("PAY24B_REFUND_PAYMENT_DATABASE_URL")
APP_URL = os.environ.get("PAY24B_REFUND_APP_DATABASE_URL")
WORKER_URL = os.environ.get("PAY24B_REFUND_WORKER_DATABASE_URL")
RECONCILIATION_URL = os.environ.get(
    "PAY24B_REFUND_RECONCILIATION_DATABASE_URL"
)

_ALL_URLS = {
    "migration": MIGRATION_URL,
    "admin": ADMIN_URL,
    "refund": REFUND_URL,
    "payment": PAYMENT_URL,
    "app": APP_URL,
    "worker": WORKER_URL,
    "reconciliation": RECONCILIATION_URL,
}

# This test deliberately upgrades, downgrades, and re-upgrades one database.
# A partial topology is skipped rather than silently substituting a broader
# identity.  When any URL is supplied, the test itself requires the complete
# isolated topology and rejects database names that do not say ``test`` or
# ``ci``.
pytestmark = pytest.mark.skipif(
    not any(_ALL_URLS.values()),
    reason="PAY-24-B refund isolated PostgreSQL 16 harness is not configured",
)


BASE_REVISION = "zz87d8e9f0a68"
REFUND_BRIDGE_REVISION = "zz97d8e9f0a69"
REFUND_BRIDGE = (
    "app_secure.pay24b_request_current_refund_admission(text,text,integer)"
)
CHECKOUT_BRIDGE = (
    "app_secure.pay24b_request_current_provider_admission(text,text,text,integer)"
)
BASE_ADMISSION = (
    "app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)"
)

ORG_ID = uuid.UUID("24b00000-0000-4000-8000-000000000071")
OPERATION_SHA = "7" * 64
LOGICAL_OPERATION_ID = "refund:pay24b-pg16-stage0:1"

RUNTIME_URLS = {
    "finance_refund_runtime": REFUND_URL,
    "finance_payment_runtime": PAYMENT_URL,
    "app_runtime": APP_URL,
    "worker_runtime": WORKER_URL,
    "finance_reconciliation_runtime": RECONCILIATION_URL,
}

RUNTIME_ROLES = tuple(RUNTIME_URLS)
DENIED_BRIDGE_ROLES = (
    "finance_payment_runtime",
    "app_runtime",
    "worker_runtime",
    "finance_reconciliation_runtime",
)
ISOLATED_ROLE_EDGES = (
    ("finance_payment_runtime", "finance_refund_runtime"),
    ("finance_refund_runtime", "finance_payment_runtime"),
    ("app_runtime", "finance_refund_runtime"),
    ("worker_runtime", "finance_refund_runtime"),
    ("finance_reconciliation_runtime", "finance_refund_runtime"),
    ("finance_refund_runtime", "finance_reconciliation_runtime"),
)


def _required_urls() -> dict[str, str]:
    missing = [name for name, value in _ALL_URLS.items() if not value]
    if missing:
        raise RuntimeError(
            "PAY-24-B refund PostgreSQL harness is incomplete: "
            + ", ".join(sorted(missing))
        )
    return {name: str(value) for name, value in _ALL_URLS.items()}


def _parsed_urls() -> dict[str, URL]:
    parsed = {name: make_url(value) for name, value in _required_urls().items()}
    coordinates = {
        (
            url.host,
            url.port,
            url.database,
            tuple(sorted(url.query.items())),
        )
        for url in parsed.values()
    }
    if len(coordinates) != 1:
        raise RuntimeError(
            "PAY-24-B refund harness identities must target one database"
        )

    database = next(iter(parsed.values())).database or ""
    if "test" not in database.lower() and "ci" not in database.lower():
        raise RuntimeError(
            "PAY-24-B refund lifecycle requires an unmistakably disposable "
            f"test/CI database, got {database!r}"
        )
    if parsed["migration"].username != "migration_owner":
        raise RuntimeError(
            "PAY-24-B refund migrations require migration_owner"
        )

    runtime_usernames = {
        parsed[name].username
        for name in (
            "refund",
            "payment",
            "app",
            "worker",
            "reconciliation",
        )
    }
    if None in runtime_usernames or len(runtime_usernames) != 5:
        raise RuntimeError(
            "PAY-24-B refund runtime identities must use five distinct logins"
        )
    if parsed["migration"].username in runtime_usernames:
        raise RuntimeError(
            "PAY-24-B refund runtime must not share migration_owner"
        )
    return parsed


def _psycopg_url(raw_url: str) -> str:
    return make_url(raw_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def _connect(raw_url: str | None, *, row_factory=None):
    assert raw_url is not None
    return psycopg.connect(
        _psycopg_url(raw_url),
        row_factory=row_factory,
    )


def _run_alembic(command: str, revision: str) -> None:
    assert MIGRATION_URL is not None
    environment = os.environ.copy()
    environment["DATABASE_URL"] = MIGRATION_URL
    environment["ENVIRONMENT"] = "test"
    environment["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-s",
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            command,
            revision,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "PAY-24-B refund Alembic lifecycle command failed: "
            f"{command} {revision}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _admin_scalar(statement: str, params: tuple[Any, ...] = ()) -> Any:
    with _connect(ADMIN_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
            assert row is not None
            return row[0]


def _revision() -> str:
    return str(_admin_scalar("SELECT version_num FROM alembic_version"))


def _function_oid(signature: str) -> int | None:
    oid = _admin_scalar("SELECT pg_catalog.to_regprocedure(%s)::oid", (signature,))
    return None if oid is None else int(oid)


def _has_execute(role: str, signature: str) -> bool:
    return bool(
        _admin_scalar(
            "SELECT pg_catalog.has_function_privilege(%s,%s,'EXECUTE')",
            (role, signature),
        )
    )


def _app_secure_snapshot() -> list[tuple[Any, ...]]:
    with _connect(ADMIN_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.proname,
                    pg_catalog.pg_get_function_identity_arguments(p.oid),
                    owner.rolname,
                    p.prosecdef,
                    p.provolatile,
                    coalesce(array_to_string(p.proconfig,E'\\n'),'') AS config,
                    pg_catalog.pg_get_functiondef(p.oid),
                    coalesce(
                        (
                            SELECT string_agg(
                                concat_ws(
                                    ':',
                                    CASE
                                        WHEN acl.grantee=0 THEN 'PUBLIC'
                                        ELSE grantee.rolname
                                    END,
                                    grantor.rolname,
                                    acl.privilege_type,
                                    acl.is_grantable::text
                                ),
                                ',' ORDER BY
                                    acl.grantee,
                                    acl.grantor,
                                    acl.privilege_type,
                                    acl.is_grantable
                            )
                            FROM pg_catalog.aclexplode(
                                coalesce(
                                    p.proacl,
                                    pg_catalog.acldefault('f',p.proowner)
                                )
                            ) AS acl
                            LEFT JOIN pg_catalog.pg_roles AS grantee
                              ON grantee.oid=acl.grantee
                            JOIN pg_catalog.pg_roles AS grantor
                              ON grantor.oid=acl.grantor
                        ),
                        ''
                    ) AS acl
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid=p.pronamespace
                JOIN pg_catalog.pg_roles AS owner
                  ON owner.oid=p.proowner
                WHERE namespace.nspname='app_secure'
                  AND p.prokind='f'
                ORDER BY
                    p.proname,
                    pg_catalog.pg_get_function_identity_arguments(p.oid)
                """
            )
            return list(cursor.fetchall())


def _bridge_snapshot() -> dict[str, Any]:
    with _connect(ADMIN_URL, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.proname,
                    pg_catalog.pg_get_function_identity_arguments(p.oid)
                        AS identity_arguments,
                    owner.rolname AS owner,
                    p.prosecdef AS security_definer,
                    p.provolatile::text AS volatility,
                    p.proconfig AS config,
                    pg_catalog.pg_get_functiondef(p.oid) AS definition,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            coalesce(
                                p.proacl,
                                pg_catalog.acldefault('f',p.proowner)
                            )
                        ) AS acl
                        WHERE acl.grantee=0
                          AND acl.privilege_type='EXECUTE'
                    ) AS public_execute,
                    coalesce(
                        (
                            SELECT array_agg(
                                concat_ws(
                                    ':',
                                    CASE
                                        WHEN acl.grantee=0 THEN 'PUBLIC'
                                        ELSE grantee.rolname
                                    END,
                                    grantor.rolname,
                                    acl.privilege_type,
                                    acl.is_grantable::text
                                )
                                ORDER BY
                                    acl.grantee,
                                    acl.grantor,
                                    acl.privilege_type,
                                    acl.is_grantable
                            )
                            FROM pg_catalog.aclexplode(
                                coalesce(
                                    p.proacl,
                                    pg_catalog.acldefault('f',p.proowner)
                                )
                            ) AS acl
                            LEFT JOIN pg_catalog.pg_roles AS grantee
                              ON grantee.oid=acl.grantee
                            JOIN pg_catalog.pg_roles AS grantor
                              ON grantor.oid=acl.grantor
                        ),
                        ARRAY[]::text[]
                    ) AS acl
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid=p.pronamespace
                JOIN pg_catalog.pg_roles AS owner
                  ON owner.oid=p.proowner
                WHERE p.oid=pg_catalog.to_regprocedure(%s)
                """,
                (REFUND_BRIDGE,),
            )
            row = cursor.fetchone()
            assert row is not None
            return dict(row)


def _assert_runtime_identity(
    raw_url: str | None,
    capability_role: str,
) -> None:
    with _connect(raw_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    session_user::text,
                    current_user::text,
                    pg_catalog.pg_has_role(
                        session_user,%s,'MEMBER'
                    ),
                    role.rolsuper,
                    role.rolcreaterole,
                    role.rolcreatedb,
                    role.rolreplication,
                    role.rolbypassrls
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname=session_user
                """,
                (capability_role,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == row[1]
            assert row[2] is True
            assert row[3:] == (False, False, False, False, False)


def _expect_denied(
    raw_url: str | None,
    statement: str,
    params: tuple[Any, ...],
    *,
    contains: str | None = None,
) -> None:
    with _connect(raw_url) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'app.current_org_id',%s,true)",
                    (str(ORG_ID),),
                )
                cursor.execute(statement, params)
        except psycopg.Error as exc:
            connection.rollback()
            assert exc.sqlstate == "42501"
            if contains is not None:
                assert contains in str(exc)
            return
        connection.rollback()
    pytest.fail("PAY-24-B refund authority call unexpectedly succeeded")


def _assert_role_isolation() -> None:
    for member, target in ISOLATED_ROLE_EDGES:
        reachable = _admin_scalar(
            """
            SELECT
                pg_catalog.pg_has_role(%s,%s,'MEMBER')
                OR pg_catalog.pg_has_role(%s,%s,'SET')
            """,
            (member, target, member, target),
        )
        assert reachable is False, f"unexpected role edge: {member} -> {target}"


def _assert_no_direct_authority_table_dml() -> None:
    for role in ("finance_refund_runtime", "finance_payment_runtime"):
        for relation in (
            "finance.payment_activation_authority",
            "finance.provider_admission_leases",
        ):
            direct_dml = _admin_scalar(
                """
                SELECT
                    pg_catalog.has_table_privilege(%s,%s,'SELECT')
                    OR pg_catalog.has_table_privilege(%s,%s,'INSERT')
                    OR pg_catalog.has_table_privilege(%s,%s,'UPDATE')
                    OR pg_catalog.has_table_privilege(%s,%s,'DELETE')
                """,
                (
                    role,
                    relation,
                    role,
                    relation,
                    role,
                    relation,
                    role,
                    relation,
                ),
            )
            assert direct_dml is False, f"direct Finance DML: {role} -> {relation}"


def test_pay24b_refund_bridge_pg16_authority_and_lifecycle() -> None:
    _parsed_urls()

    assert 160000 <= int(_admin_scalar("SHOW server_version_num")) < 170000
    assert _revision() == BASE_REVISION
    assert _function_oid(REFUND_BRIDGE) is None

    for role, url in RUNTIME_URLS.items():
        _assert_runtime_identity(url, role)

    predecessor_snapshot = _app_secure_snapshot()
    predecessor_checkout_definition = _admin_scalar(
        "SELECT pg_catalog.pg_get_functiondef(%s::regprocedure)",
        (CHECKOUT_BRIDGE,),
    )
    assert not _has_execute("finance_refund_runtime", CHECKOUT_BRIDGE)
    _assert_role_isolation()

    first_bridge_snapshot: dict[str, Any] | None = None
    lifecycle_complete = False
    try:
        _run_alembic("upgrade", "head")
        assert _revision() == REFUND_BRIDGE_REVISION

        first_bridge_snapshot = _bridge_snapshot()
        assert first_bridge_snapshot["owner"] == "app_security_owner"
        assert first_bridge_snapshot["security_definer"] is True
        assert first_bridge_snapshot["volatility"] == "v"
        assert set(first_bridge_snapshot["config"] or ()) == {
            "search_path=pg_catalog",
            "row_security=on",
        }
        assert first_bridge_snapshot["public_execute"] is False

        definition = str(first_bridge_snapshot["definition"])
        assert "'finance_refund_runtime'" in definition
        assert "'refund_execution'" in definition
        assert "pay24a_request_provider_admission" in definition
        assert "p_capability" not in definition
        assert "'finance_payment_runtime'" not in definition

        for role in RUNTIME_ROLES:
            assert _has_execute(role, REFUND_BRIDGE) is (
                role == "finance_refund_runtime"
            )
        assert not _has_execute("finance_refund_runtime", CHECKOUT_BRIDGE)
        _assert_role_isolation()
        _assert_no_direct_authority_table_dml()

        posture = _admin_scalar(
            """
            SELECT pg_catalog.jsonb_build_array(
                stage,
                provider_egress_state,
                refund_execution
            )
            FROM finance.payment_activation_authority
            WHERE singleton
            """
        )
        assert posture == [0, "blocked", False]

        admission_count_before = int(
            _admin_scalar(
                "SELECT count(*) FROM finance.provider_admission_leases"
            )
        )
        request_statement = (
            "SELECT * FROM "
            "app_secure.pay24b_request_current_refund_admission(%s,%s,%s)"
        )
        request_params = (
            LOGICAL_OPERATION_ID,
            OPERATION_SHA,
            300,
        )

        _expect_denied(
            REFUND_URL,
            request_statement,
            request_params,
            contains="PAY-24-A provider admission authority denied",
        )
        for role in DENIED_BRIDGE_ROLES:
            _expect_denied(
                RUNTIME_URLS[role],
                request_statement,
                request_params,
            )

        # PAY24-A's shared lower-level function is executable by the two
        # provider runtimes, so its capability-specific body guard is part of
        # the peer-isolation proof as well as the new bridge ACL.
        generation = int(
            _admin_scalar(
                "SELECT generation FROM finance.payment_activation_authority "
                "WHERE singleton"
            )
        )
        assert _has_execute("finance_payment_runtime", BASE_ADMISSION)
        _expect_denied(
            PAYMENT_URL,
            "SELECT * FROM "
            "app_secure.pay24a_request_provider_admission(%s,%s,%s,%s,%s)",
            (
                generation,
                "refund_execution",
                LOGICAL_OPERATION_ID,
                OPERATION_SHA,
                300,
            ),
            contains=(
                "PAY-24-A refund admission requires finance_refund_runtime"
            ),
        )

        # The refund identity must not use the independently certified checkout
        # bridge, even when it supplies refund_execution as the capability.
        _expect_denied(
            REFUND_URL,
            "SELECT * FROM "
            "app_secure.pay24b_request_current_provider_admission("
            "%s,%s,%s,%s)",
            (
                "refund_execution",
                LOGICAL_OPERATION_ID,
                OPERATION_SHA,
                300,
            ),
        )
        admission_count_after = int(
            _admin_scalar(
                "SELECT count(*) FROM finance.provider_admission_leases"
            )
        )
        assert admission_count_after == admission_count_before

        _run_alembic("downgrade", BASE_REVISION)
        assert _revision() == BASE_REVISION
        assert _function_oid(REFUND_BRIDGE) is None
        assert _app_secure_snapshot() == predecessor_snapshot
        assert _admin_scalar(
            "SELECT pg_catalog.pg_get_functiondef(%s::regprocedure)",
            (CHECKOUT_BRIDGE,),
        ) == predecessor_checkout_definition
        assert not _has_execute("finance_refund_runtime", CHECKOUT_BRIDGE)
        _assert_role_isolation()

        _run_alembic("upgrade", "head")
        assert _revision() == REFUND_BRIDGE_REVISION
        assert _bridge_snapshot() == first_bridge_snapshot
        assert not _has_execute("finance_refund_runtime", CHECKOUT_BRIDGE)
        _assert_role_isolation()
        _assert_no_direct_authority_table_dml()
        lifecycle_complete = True
    finally:
        # Failure cleanup is deliberately bounded to the new non-production
        # bridge revision.  Never downgrade or drop the disposable database.
        if (
            first_bridge_snapshot is not None
            and _revision() == BASE_REVISION
        ):
            _run_alembic("upgrade", "head")

    assert lifecycle_complete
