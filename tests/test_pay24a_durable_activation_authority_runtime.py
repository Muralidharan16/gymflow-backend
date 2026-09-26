from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
import threading
import time
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest


CONFIG_URL = os.environ.get("PAY24A_CONFIG_DATABASE_URL")
PAYMENT_URL = os.environ.get("PAY24A_PAYMENT_DATABASE_URL")
REFUND_URL = os.environ.get("PAY24A_REFUND_DATABASE_URL")
MAINTENANCE_URL = os.environ.get("PAY24A_MAINTENANCE_DATABASE_URL")
APP_URL = os.environ.get("PAY24A_APP_DATABASE_URL")
ATTACK_URL = os.environ.get("PAY24A_ATTACK_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY24A_ADMIN_DATABASE_URL")

_ALL_URLS = (
    CONFIG_URL,
    PAYMENT_URL,
    REFUND_URL,
    MAINTENANCE_URL,
    APP_URL,
    ATTACK_URL,
    ADMIN_URL,
)

# This is an isolated, destructive-by-accumulation certification harness. It
# expects one fresh disposable database at zz57d8e9f0a65. A partial harness is
# an error: otherwise an omitted attack identity could hide an ACL regression.
pytestmark = pytest.mark.skipif(
    not any(_ALL_URLS),
    reason="PAY-24-A isolated PostgreSQL 16 harness is not configured",
)


ORG = uuid.UUID("24a00000-0000-4000-8000-000000000001")
OTHER_ORG = uuid.UUID("24a00000-0000-4000-8000-000000000002")

SHA_A = "a" * 40
SHA_B = "b" * 40
OPERATION_SHA_A = "1" * 64
OPERATION_SHA_B = "2" * 64

ACTOR = "pay24a.synthetic-test-operator"
MEASURER = "pay24a.synthetic-release-attestor"
AUTHORIZER = "pay24a.synthetic-human-authorizer"


def _required(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _set_org(cur: psycopg.Cursor, organization_id: uuid.UUID) -> None:
    cur.execute(
        "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
        (str(organization_id),),
    )


def _fetchone(
    url: str,
    statement: str,
    params: tuple[object, ...] = (),
    *,
    organization_id: uuid.UUID | None = None,
) -> dict[str, object]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if organization_id is not None:
                _set_org(cur, organization_id)
            cur.execute(statement, params)
            row = cur.fetchone()
            assert row is not None
        conn.commit()
    return row


def _fetchall(
    url: str,
    statement: str,
    params: tuple[object, ...] = (),
    *,
    organization_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if organization_id is not None:
                _set_org(cur, organization_id)
            cur.execute(statement, params)
            rows = cur.fetchall()
        conn.commit()
    return rows


def _expect_error(
    url: str,
    statement: str,
    params: tuple[object, ...] = (),
    *,
    sqlstate: str,
    contains: str,
    organization_id: uuid.UUID | None = None,
) -> psycopg.Error:
    with psycopg.connect(url) as conn:
        try:
            with conn.cursor() as cur:
                if organization_id is not None:
                    _set_org(cur, organization_id)
                cur.execute(statement, params)
        except psycopg.Error as exc:
            conn.rollback()
            assert exc.sqlstate == sqlstate
            assert contains in str(exc)
            return exc
        conn.rollback()
    pytest.fail(f"statement unexpectedly succeeded: {statement}")


_BIND_RELEASE_SQL = """
    SELECT * FROM app_secure.pay24a_bind_release_identity(
        %s,%s,%s,%s,%s,%s,%s
    )
"""
_BIND_AUTHORIZATION_SQL = """
    SELECT * FROM app_secure.pay24a_bind_human_authorization(
        %s,%s,%s,%s,%s,%s,%s,%s
    )
"""
_TRANSITION_SQL = """
    SELECT * FROM app_secure.pay24a_transition_activation(
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
    )
"""
_REQUEST_ADMISSION_SQL = """
    SELECT *
    FROM app_secure.pay24a_request_provider_admission(%s,%s,%s,%s,%s)
"""
_START_ADMISSION_SQL = """
    SELECT * FROM app_secure.pay24a_start_provider_admission(%s,%s)
"""
_FINISH_ADMISSION_SQL = """
    SELECT * FROM app_secure.pay24a_finish_provider_admission(%s,%s,%s)
"""


def _snapshot() -> dict[str, object]:
    return _fetchone(
        _required(CONFIG_URL, "PAY24A_CONFIG_DATABASE_URL"),
        "SELECT * FROM app_secure.pay24a_activation_snapshot()",
    )


def _transition_params(
    operation_id: uuid.UUID,
    generation: int,
    *,
    stage: int,
    egress: str,
    organization_id: uuid.UUID | None,
    checkout: bool,
    webhooks: bool = False,
    payment_application: bool = False,
    subscription_activation: bool = False,
    refund_execution: bool = False,
    recurring_billing: bool = False,
    dunning: bool = False,
    platform_billing: bool = False,
) -> tuple[object, ...]:
    return (
        operation_id,
        generation,
        stage,
        egress,
        organization_id,
        checkout,
        webhooks,
        payment_application,
        subscription_activation,
        refund_execution,
        recurring_billing,
        dunning,
        platform_billing,
        ACTOR,
    )


def _request_admission(
    logical_operation_id: str,
    generation: int,
    *,
    operation_sha: str = OPERATION_SHA_A,
    lease_seconds: int = 300,
    organization_id: uuid.UUID = ORG,
    capability: str = "checkout",
    url: str | None = None,
) -> dict[str, object]:
    return _fetchone(
        url or _required(PAYMENT_URL, "PAY24A_PAYMENT_DATABASE_URL"),
        _REQUEST_ADMISSION_SQL,
        (
            generation,
            capability,
            logical_operation_id,
            operation_sha,
            lease_seconds,
        ),
        organization_id=organization_id,
    )


def _start_admission(
    admission_id: uuid.UUID,
    execution_id: uuid.UUID,
    *,
    organization_id: uuid.UUID = ORG,
) -> dict[str, object]:
    return _fetchone(
        _required(PAYMENT_URL, "PAY24A_PAYMENT_DATABASE_URL"),
        _START_ADMISSION_SQL,
        (admission_id, execution_id),
        organization_id=organization_id,
    )


def _finish_admission(
    admission_id: uuid.UUID,
    execution_id: uuid.UUID,
    outcome: str,
) -> dict[str, object]:
    return _fetchone(
        _required(PAYMENT_URL, "PAY24A_PAYMENT_DATABASE_URL"),
        _FINISH_ADMISSION_SQL,
        (admission_id, execution_id, outcome),
        organization_id=ORG,
    )


def _sleep_until(moment: datetime) -> None:
    delay = (moment - datetime.now(UTC)).total_seconds() + 0.15
    if delay > 0:
        time.sleep(delay)


def _seed_synthetic_organizations() -> None:
    with psycopg.connect(
        _required(ADMIN_URL, "PAY24A_ADMIN_DATABASE_URL")
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(id,name,tier,is_active)
                VALUES
                    (%s,'PAY24A Synthetic Internal','basic',true),
                    (%s,'PAY24A Synthetic Other','basic',true)
                """,
                (ORG, OTHER_ORG),
            )
        conn.commit()


def _assert_stage_zero_constraints_are_database_enforced() -> None:
    admin_url = _required(ADMIN_URL, "PAY24A_ADMIN_DATABASE_URL")
    constraint_name = "chk_pay24a_authority_stage_posture"

    for assignment in ("provider_egress_state='open'", "checkout=true"):
        with psycopg.connect(admin_url) as conn:
            try:
                with conn.cursor() as cur:
                    # Do not disable triggers, FKs, RLS, or other integrity
                    # enforcement on the real PAY-24-A authority table.
                    #
                    # The real table deliberately has an immutable-row/update
                    # guard. This probe is testing the independent Stage-0
                    # CHECK invariant, so obtain the exact validated CHECK
                    # definition from the live PostgreSQL catalog and exercise
                    # that definition on a transaction-local table.
                    cur.execute(
                        """
                        SELECT
                            c.convalidated,
                            pg_catalog.pg_get_constraintdef(c.oid, true)
                        FROM pg_catalog.pg_constraint AS c
                        WHERE c.conrelid =
                              'finance.payment_activation_authority'::regclass
                          AND c.conname = %s
                          AND c.contype = 'c'
                        """,
                        (constraint_name,),
                    )

                    constraint = cur.fetchone()
                    assert constraint is not None
                    assert constraint[0] is True

                    constraint_definition = constraint[1]
                    assert constraint_definition.startswith("CHECK ")

                    cur.execute(
                        """
                        CREATE TEMP TABLE pay24a_authority_constraint_probe
                        (
                            LIKE finance.payment_activation_authority
                        )
                        ON COMMIT DROP
                        """
                    )

                    cur.execute(
                        "ALTER TABLE pay24a_authority_constraint_probe "
                        f"ADD CONSTRAINT {constraint_name} "
                        f"{constraint_definition}"
                    )

                    cur.execute(
                        """
                        INSERT INTO pay24a_authority_constraint_probe
                        SELECT *
                        FROM finance.payment_activation_authority
                        WHERE singleton
                        """
                    )

                    cur.execute(
                        "UPDATE pay24a_authority_constraint_probe "
                        f"SET {assignment} WHERE singleton"
                    )

            except psycopg.errors.CheckViolation as exc:
                assert exc.sqlstate == "23514"
                assert exc.diag.constraint_name == constraint_name
                conn.rollback()
            else:
                conn.rollback()
                pytest.fail(
                    "Stage-0 impossible posture escaped its database constraint"
                )


def test_pay24a_postgresql16_adversarial_authority_protocol() -> None:
    config_url = _required(CONFIG_URL, "PAY24A_CONFIG_DATABASE_URL")
    payment_url = _required(PAYMENT_URL, "PAY24A_PAYMENT_DATABASE_URL")
    refund_url = _required(REFUND_URL, "PAY24A_REFUND_DATABASE_URL")
    maintenance_url = _required(
        MAINTENANCE_URL, "PAY24A_MAINTENANCE_DATABASE_URL"
    )
    admin_url = _required(ADMIN_URL, "PAY24A_ADMIN_DATABASE_URL")

    with psycopg.connect(admin_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            assert 160000 <= int(cur.fetchone()[0]) < 170000
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()[0] == "zz57d8e9f0a65"

    initial = _snapshot()
    assert initial == {
        "stage": 0,
        "generation": 0,
        "provider_egress": "blocked",
        "enabled_capabilities": [],
        "internal_organization_id": None,
        "authorization_id": None,
        "authorized_stage": None,
        "certified_sha": None,
        "deployed_sha": None,
        "updated_at": initial["updated_at"],
        "posture_digest": initial["posture_digest"],
    }
    assert len(str(initial["posture_digest"])) == 64
    _seed_synthetic_organizations()
    _assert_stage_zero_constraints_are_database_enforced()

    failed_operation_ids: list[uuid.UUID] = []

    # Missing trusted release identity and missing human authorization fail
    # closed. Stage 0 cannot represent egress or an enabled capability.
    op_missing_authority = uuid.uuid4()
    failed_operation_ids.append(op_missing_authority)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_missing_authority,
            0,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )
    for egress, checkout in (("open", False), ("blocked", True)):
        operation_id = uuid.uuid4()
        failed_operation_ids.append(operation_id)
        _expect_error(
            config_url,
            _TRANSITION_SQL,
            _transition_params(
                operation_id,
                0,
                stage=0,
                egress=egress,
                organization_id=None,
                checkout=checkout,
            ),
            sqlstate="23514",
            contains="Stage 0 posture is fail-closed",
        )
    assert _snapshot()["generation"] == 0

    measured_at = datetime.now(UTC) - timedelta(minutes=2)
    release_operation = uuid.uuid4()
    release = _fetchone(
        config_url,
        _BIND_RELEASE_SQL,
        (
            release_operation,
            0,
            SHA_A,
            SHA_A,
            MEASURER,
            measured_at,
            ACTOR,
        ),
    )
    assert release["generation"] == 1

    # Measurement time is attestation metadata, not transition input. A retry
    # with the same binding returns the original durable result even when the
    # trusted measurer's observation timestamp moved forward.
    release_retry = _fetchone(
        config_url,
        _BIND_RELEASE_SQL,
        (
            release_operation,
            0,
            SHA_A,
            SHA_A,
            MEASURER,
            measured_at + timedelta(minutes=1),
            ACTOR,
        ),
    )
    assert release_retry == release
    assert _snapshot()["generation"] == 1

    op_missing_authorization = uuid.uuid4()
    failed_operation_ids.append(op_missing_authorization)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_missing_authorization,
            1,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )

    malformed_authorization_operation = uuid.uuid4()
    failed_operation_ids.append(malformed_authorization_operation)
    _expect_error(
        config_url,
        _BIND_AUTHORIZATION_SQL,
        (
            malformed_authorization_operation,
            1,
            "bad authorization id",
            "not-a-sha",
            1,
            AUTHORIZER,
            datetime.now(UTC),
            ACTOR,
        ),
        sqlstate="22023",
        contains="human authorization invalid",
    )

    authorization_time = datetime.now(UTC) - timedelta(minutes=1)
    stage_zero_authorization = _fetchone(
        config_url,
        _BIND_AUTHORIZATION_SQL,
        (
            uuid.uuid4(),
            1,
            "pay24a-test-stage0",
            SHA_A,
            0,
            AUTHORIZER,
            authorization_time,
            ACTOR,
        ),
    )
    assert stage_zero_authorization == {
        "generation": 2,
        "authorization_id": "pay24a-test-stage0",
        "authorized_stage": 0,
    }

    op_wrong_stage = uuid.uuid4()
    failed_operation_ids.append(op_wrong_stage)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_wrong_stage,
            2,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )

    wrong_sha_authorization = _fetchone(
        config_url,
        _BIND_AUTHORIZATION_SQL,
        (
            uuid.uuid4(),
            2,
            "pay24a-test-wrong-sha",
            SHA_B,
            1,
            AUTHORIZER,
            authorization_time,
            ACTOR,
        ),
    )
    assert wrong_sha_authorization["generation"] == 3
    op_wrong_authorized_sha = uuid.uuid4()
    failed_operation_ids.append(op_wrong_authorized_sha)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_wrong_authorized_sha,
            3,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )

    mismatched_release = _fetchone(
        config_url,
        _BIND_RELEASE_SQL,
        (
            uuid.uuid4(),
            3,
            SHA_A,
            SHA_B,
            MEASURER,
            measured_at,
            ACTOR,
        ),
    )
    assert mismatched_release["generation"] == 4
    valid_authorization = _fetchone(
        config_url,
        _BIND_AUTHORIZATION_SQL,
        (
            uuid.uuid4(),
            4,
            "pay24a-test-valid-stage1",
            SHA_A,
            1,
            AUTHORIZER,
            authorization_time,
            ACTOR,
        ),
    )
    assert valid_authorization["generation"] == 5

    op_deployed_mismatch = uuid.uuid4()
    failed_operation_ids.append(op_deployed_mismatch)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_deployed_mismatch,
            5,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )

    matched_release = _fetchone(
        config_url,
        _BIND_RELEASE_SQL,
        (
            uuid.uuid4(),
            5,
            SHA_A,
            SHA_A,
            MEASURER,
            measured_at,
            ACTOR,
        ),
    )
    assert matched_release["generation"] == 6

    op_missing_internal_org = uuid.uuid4()
    failed_operation_ids.append(op_missing_internal_org)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_missing_internal_org,
            6,
            stage=1,
            egress="open",
            organization_id=None,
            checkout=True,
        ),
        sqlstate="23514",
        contains="Stage 1 exact authority is incomplete",
    )
    op_skipped_stage = uuid.uuid4()
    failed_operation_ids.append(op_skipped_stage)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            op_skipped_stage,
            6,
            stage=2,
            egress="open",
            organization_id=ORG,
            checkout=True,
        ),
        sqlstate="22023",
        contains="activation transition invalid",
    )

    # Two transactions race on the singleton generation. Exactly one may
    # advance generation 6 to 7; the loser observes a stale generation.
    transition_barrier = threading.Barrier(2)
    transition_operations = (uuid.uuid4(), uuid.uuid4())

    def race_transition(operation_id: uuid.UUID):
        transition_barrier.wait()
        try:
            row = _fetchone(
                config_url,
                _TRANSITION_SQL,
                _transition_params(
                    operation_id,
                    6,
                    stage=1,
                    egress="open",
                    organization_id=ORG,
                    checkout=True,
                    webhooks=True,
                    payment_application=True,
                    subscription_activation=True,
                ),
            )
            return "ok", operation_id, row
        except psycopg.Error as exc:
            return exc.sqlstate, operation_id, str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        transition_results = list(
            pool.map(race_transition, transition_operations)
        )
    assert sorted(result[0] for result in transition_results) == ["40001", "ok"]
    winner = next(result for result in transition_results if result[0] == "ok")
    loser = next(result for result in transition_results if result[0] == "40001")
    winning_transition_id = winner[1]
    failed_operation_ids.append(loser[1])
    winning_transition = winner[2]
    assert winning_transition["generation"] == 7
    assert winning_transition["stage"] == 1
    assert winning_transition["provider_egress"] == "open"

    # Exact operation replay is deterministic even though its expected
    # generation is now stale. Changing the same operation is a conflict.
    replay = _fetchone(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            winning_transition_id,
            6,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
            webhooks=True,
            payment_application=True,
            subscription_activation=True,
        ),
    )
    assert replay == winning_transition
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            winning_transition_id,
            6,
            stage=1,
            egress="blocked",
            organization_id=ORG,
            checkout=True,
            webhooks=True,
            payment_application=True,
            subscription_activation=True,
        ),
        sqlstate="23505",
        contains="operation id conflicts",
    )

    stage_one = _snapshot()
    assert stage_one["generation"] == 7
    assert stage_one["stage"] == 1
    assert stage_one["provider_egress"] == "open"
    assert stage_one["internal_organization_id"] == ORG
    assert stage_one["authorization_id"] == "pay24a-test-valid-stage1"
    assert stage_one["authorized_stage"] == 1
    assert stage_one["certified_sha"] == stage_one["deployed_sha"] == SHA_A
    assert stage_one["enabled_capabilities"] == [
        "checkout",
        "webhooks",
        "payment_application",
        "subscription_activation",
    ]

    # Organization, generation, capability, and provider-egress checks are all
    # evaluated in PostgreSQL before a durable lease can be created.
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (7, "checkout", "wrong-tenant", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="provider admission authority denied",
        organization_id=OTHER_ORG,
    )
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (7, "checkout", "missing-context", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="requires organization context",
    )
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (7, "webhooks", "no-concrete-runtime", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="no provider-admission runtime",
        organization_id=ORG,
    )
    _expect_error(
        refund_url,
        _REQUEST_ADMISSION_SQL,
        (7, "refund_execution", "refund-disabled", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="capability disabled",
        organization_id=ORG,
    )
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (6, "checkout", "stale-generation", OPERATION_SHA_A, 300),
        sqlstate="40001",
        contains="stale activation generation",
        organization_id=ORG,
    )

    block_transition = _fetchone(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            uuid.uuid4(),
            7,
            stage=1,
            egress="blocked",
            organization_id=ORG,
            checkout=True,
            webhooks=True,
            payment_application=True,
            subscription_activation=True,
        ),
    )
    assert block_transition["generation"] == 8
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (8, "checkout", "egress-blocked", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="provider admission authority denied",
        organization_id=ORG,
    )
    reopen_transition = _fetchone(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            uuid.uuid4(),
            8,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
            webhooks=True,
            payment_application=True,
            subscription_activation=True,
        ),
    )
    assert reopen_transition["generation"] == 9

    idempotent = _request_admission("checkout:idempotent", 9)
    idempotent_replay = _request_admission("checkout:idempotent", 9)
    assert idempotent_replay == idempotent
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (9, "checkout", "checkout:idempotent", OPERATION_SHA_B, 300),
        sqlstate="23505",
        contains="logical provider operation conflicts",
        organization_id=ORG,
    )

    # A lease is only outbound authority after its request transaction commits.
    # Request-then-start in one transaction fails and rolls the lease back.
    uncommitted_logical_id = "checkout:uncommitted"
    with psycopg.connect(payment_url, row_factory=dict_row) as conn:
        try:
            with conn.cursor() as cur:
                _set_org(cur, ORG)
                cur.execute(
                    _REQUEST_ADMISSION_SQL,
                    (
                        9,
                        "checkout",
                        uncommitted_logical_id,
                        OPERATION_SHA_A,
                        300,
                    ),
                )
                uncommitted = cur.fetchone()
                assert uncommitted is not None
                cur.execute(
                    _START_ADMISSION_SQL,
                    (uncommitted["admission_id"], uuid.uuid4()),
                )
        except psycopg.Error as exc:
            conn.rollback()
            assert exc.sqlstate == "55000"
            assert "must commit before start" in str(exc)
        else:
            conn.rollback()
            pytest.fail("an uncommitted admission was startable")
    with psycopg.connect(admin_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM finance.provider_admission_leases "
                "WHERE logical_operation_id=%s",
                (uncommitted_logical_id,),
            )
            assert cur.fetchone()[0] == 0

    admission_barrier = threading.Barrier(2)

    def race_duplicate_admission():
        admission_barrier.wait()
        return _request_admission("checkout:concurrent", 9)

    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate_results = list(
            pool.map(lambda _: race_duplicate_admission(), range(2))
        )
    assert duplicate_results[0] == duplicate_results[1]
    concurrent_admission = duplicate_results[0]
    with psycopg.connect(admin_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM finance.provider_admission_leases "
                "WHERE organization_id=%s AND capability='checkout' "
                "AND logical_operation_id='checkout:concurrent'",
                (ORG,),
            )
            assert cur.fetchone()[0] == 1

    expired_admission = _request_admission(
        "checkout:expires", 9, lease_seconds=5
    )
    _sleep_until(expired_admission["lease_expires_at"])
    expired_start = _start_admission(
        expired_admission["admission_id"], uuid.uuid4()
    )
    assert expired_start["state"] == "expired"
    expired_replay = _request_admission(
        "checkout:expires", 9, lease_seconds=5
    )
    assert expired_replay["admission_id"] == expired_admission["admission_id"]
    assert expired_replay["state"] == "expired"

    completed = _request_admission("checkout:completed", 9)
    completed_execution = uuid.uuid4()
    completed_start = _start_admission(
        completed["admission_id"], completed_execution
    )
    assert completed_start["state"] == "active"
    _expect_error(
        payment_url,
        _START_ADMISSION_SQL,
        (completed["admission_id"], completed_execution),
        sqlstate="55000",
        contains="provider execution already started",
        organization_id=ORG,
    )
    completed_finish = _finish_admission(
        completed["admission_id"], completed_execution, "completed"
    )
    assert completed_finish["state"] == "completed"
    assert (
        _finish_admission(
            completed["admission_id"], completed_execution, "completed"
        )
        == completed_finish
    )

    crashed = _request_admission(
        "checkout:crashed-active", 9, lease_seconds=5
    )
    crashed_execution = uuid.uuid4()
    crashed_start = _start_admission(
        crashed["admission_id"], crashed_execution
    )
    assert crashed_start["state"] == "active"
    awaiting_rollback = _request_admission("checkout:rollback-revoke", 9)

    drain_before = _fetchall(
        config_url,
        "SELECT * FROM app_secure.pay24a_admission_drain_snapshot()",
    )
    assert any(row["state"] == "active" for row in drain_before)
    assert any(row["state"] == "admitted" for row in drain_before)

    rollback_operation = uuid.uuid4()
    rollback = _fetchone(
        config_url,
        "SELECT * FROM app_secure.pay24a_begin_emergency_rollback(%s,%s,%s)",
        (rollback_operation, 9, ACTOR),
    )
    assert rollback["generation"] == 10
    assert rollback["active_admission_count"] == 1
    assert rollback["revoked_admission_count"] == 3

    # CLOSING is one-way. New ordinary posture/release/authorization mutations
    # are denied until rollback finalization installs Stage 0.
    closing_transition_operation = uuid.uuid4()
    failed_operation_ids.append(closing_transition_operation)
    _expect_error(
        config_url,
        _TRANSITION_SQL,
        _transition_params(
            closing_transition_operation,
            10,
            stage=1,
            egress="open",
            organization_id=ORG,
            checkout=True,
            webhooks=True,
            payment_application=True,
            subscription_activation=True,
        ),
        sqlstate="55000",
        contains="ordinary transition denied",
    )
    closing_release_operation = uuid.uuid4()
    failed_operation_ids.append(closing_release_operation)
    _expect_error(
        config_url,
        _BIND_RELEASE_SQL,
        (
            closing_release_operation,
            10,
            SHA_A,
            SHA_A,
            MEASURER,
            measured_at,
            ACTOR,
        ),
        sqlstate="55000",
        contains="release rebind denied",
    )
    closing_authorization_operation = uuid.uuid4()
    failed_operation_ids.append(closing_authorization_operation)
    _expect_error(
        config_url,
        _BIND_AUTHORIZATION_SQL,
        (
            closing_authorization_operation,
            10,
            "pay24a-test-closing-auth",
            SHA_A,
            1,
            AUTHORIZER,
            authorization_time,
            ACTOR,
        ),
        sqlstate="55000",
        contains="authorization rebind denied",
    )
    assert _snapshot()["generation"] == 10
    assert _snapshot()["provider_egress"] == "closing"

    # Rollback linearizes closure first: generation 10 sees CLOSING,
    # generation 9 is stale, and old admitted leases are already revoked.
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (10, "checkout", "after-rollback", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="provider admission authority denied",
        organization_id=ORG,
    )
    _expect_error(
        payment_url,
        _REQUEST_ADMISSION_SQL,
        (9, "checkout", "stale-after-rollback", OPERATION_SHA_A, 300),
        sqlstate="40001",
        contains="stale activation generation",
        organization_id=ORG,
    )
    for revoked in (idempotent, concurrent_admission, awaiting_rollback):
        _expect_error(
            payment_url,
            _START_ADMISSION_SQL,
            (revoked["admission_id"], uuid.uuid4()),
            sqlstate="55000",
            contains="not startable",
            organization_id=ORG,
        )

    finalize_operation = uuid.uuid4()
    failed_operation_ids.append(finalize_operation)
    _expect_error(
        config_url,
        "SELECT * FROM app_secure.pay24a_finalize_emergency_rollback(%s,%s,%s)",
        (finalize_operation, 10, ACTOR),
        sqlstate="55000",
        contains="rollback drain is incomplete",
    )
    assert _snapshot()["generation"] == 10
    assert _snapshot()["provider_egress"] == "closing"

    # A worker crash leaves ACTIVE authority visible and blocks final closure.
    # Expiry turns it into UNKNOWN, never a reusable provider authorization.
    _sleep_until(crashed["lease_expires_at"])
    expiry_operation = uuid.uuid4()
    expired_counts = _fetchone(
        maintenance_url,
        "SELECT * FROM app_secure.pay24a_expire_provider_admissions(%s,%s)",
        (expiry_operation, ACTOR),
    )
    assert expired_counts["expired_count"] == 0
    assert expired_counts["unknown_count"] == 1
    assert _fetchone(
        maintenance_url,
        "SELECT * FROM app_secure.pay24a_expire_provider_admissions(%s,%s)",
        (expiry_operation, ACTOR),
    ) == expired_counts

    finalized = _fetchone(
        config_url,
        "SELECT * FROM app_secure.pay24a_finalize_emergency_rollback(%s,%s,%s)",
        (uuid.uuid4(), 10, ACTOR),
    )
    assert finalized == {
        "generation": 11,
        "stage": 0,
        "provider_egress": "blocked",
    }
    final = _snapshot()
    assert final["generation"] == 11
    assert final["stage"] == 0
    assert final["provider_egress"] == "blocked"
    assert final["enabled_capabilities"] == []
    assert final["internal_organization_id"] is None
    assert final["authorization_id"] is None
    assert final["authorized_stage"] is None
    assert final["certified_sha"] == final["deployed_sha"] == SHA_A

    drain_after = _fetchall(
        maintenance_url,
        "SELECT * FROM app_secure.pay24a_admission_drain_snapshot()",
    )
    assert not any(
        row["state"] in {"admitted", "active"} for row in drain_after
    )
    assert any(
        row["state"] == "unknown" and row["lease_count"] == 1
        for row in drain_after
    )

    # Evidence is atomic with each generation advance. Failed operations leave
    # neither a generation nor a misleading success event.
    evidence = _fetchall(
        config_url,
        "SELECT * FROM app_secure.pay24a_transition_evidence(%s,%s)",
        (0, 100),
    )
    assert [row["event_sequence"] for row in evidence] == list(
        range(1, len(evidence) + 1)
    )
    assert evidence[0]["previous_event_hash"] is None
    for previous, current in zip(evidence, evidence[1:]):
        assert current["previous_event_hash"] == previous["event_hash"]
    generation_events = [
        row
        for row in evidence
        if row["new_generation"] == row["prior_generation"] + 1
    ]
    assert [row["new_generation"] for row in generation_events] == list(
        range(1, 12)
    )
    assert len({row["new_generation"] for row in generation_events}) == 11
    assert sum(
        row["operation_id"] == release_operation for row in evidence
    ) == 1
    assert any(
        row["event_type"] == "provider_admissions_expired"
        and row["new_generation"] == row["prior_generation"] == 10
        for row in evidence
    )
    assert evidence[-1]["event_type"] == "emergency_rollback_finalized"
    assert evidence[-1]["posture_digest"] == final["posture_digest"]

    with psycopg.connect(admin_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM finance.payment_activation_transition_events
                WHERE operation_id = ANY(%s::uuid[])
                """,
                ([str(value) for value in failed_operation_ids],),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT state FROM finance.provider_admission_leases "
                "WHERE admission_id=%s",
                (crashed["admission_id"],),
            )
            assert cur.fetchone()[0] == "unknown"
            cur.execute(
                "SELECT state FROM finance.provider_admission_leases "
                "WHERE admission_id=%s",
                (awaiting_rollback["admission_id"],),
            )
            assert cur.fetchone()[0] == "revoked"


def test_pay24a_database_acl_rls_and_public_boundary() -> None:
    admin_url = _required(ADMIN_URL, "PAY24A_ADMIN_DATABASE_URL")
    app_url = _required(APP_URL, "PAY24A_APP_DATABASE_URL")
    attack_url = _required(ATTACK_URL, "PAY24A_ATTACK_DATABASE_URL")
    config_url = _required(CONFIG_URL, "PAY24A_CONFIG_DATABASE_URL")
    payment_url = _required(PAYMENT_URL, "PAY24A_PAYMENT_DATABASE_URL")

    with psycopg.connect(admin_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,
                       c.relowner::regrole::text AS owner
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
                WHERE n.nspname='finance'
                  AND c.relname IN (
                      'payment_activation_release_identities',
                      'payment_activation_authorizations',
                      'payment_activation_authority',
                      'payment_activation_transition_events',
                      'provider_admission_leases'
                  )
                ORDER BY c.relname
                """
            )
            tables = cur.fetchall()
            assert len(tables) == 5
            assert all(row["relrowsecurity"] for row in tables)
            assert all(row["relforcerowsecurity"] for row in tables)
            assert {row["owner"] for row in tables} == {"migration_owner"}

            cur.execute(
                """
                SELECT p.proname,p.prosecdef,p.proowner::regrole::text AS owner,
                       p.proconfig,
                       pg_catalog.has_function_privilege(
                           'public',p.oid,'EXECUTE'
                       ) AS public_execute
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname LIKE 'pay24a_%'
                ORDER BY p.proname
                """
            )
            functions = cur.fetchall()
            assert len(functions) == 19
            assert all(row["prosecdef"] for row in functions)
            assert {row["owner"] for row in functions} == {
                "app_security_owner"
            }
            assert not any(row["public_execute"] for row in functions)
            for row in functions:
                assert "search_path=pg_catalog" in row["proconfig"]
                assert "row_security=on" in row["proconfig"]

            cur.execute(
                """
                SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,
                       rolreplication,rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname IN (
                    'finance_config_runtime','finance_payment_runtime',
                    'finance_refund_runtime','lifecycle_maintenance_runtime',
                    'app_runtime','worker_runtime'
                )
                """
            )
            roles = cur.fetchall()
            assert len(roles) == 6
            for role in roles:
                assert not role["rolsuper"]
                assert not role["rolcreatedb"]
                assert not role["rolcreaterole"]
                assert not role["rolreplication"]
                assert not role["rolbypassrls"]

            cur.execute(
                """
                SELECT role_name,table_name,
                       pg_catalog.has_table_privilege(
                           role_name,
                           'finance.' || table_name,
                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                       ) AS any_table_privilege
                FROM unnest(ARRAY[
                    'finance_config_runtime','finance_payment_runtime',
                    'finance_refund_runtime','finance_read_runtime',
                    'finance_maintenance_runtime',
                    'lifecycle_maintenance_runtime','app_runtime',
                    'worker_runtime'
                ]) AS role_name
                CROSS JOIN unnest(ARRAY[
                    'payment_activation_release_identities',
                    'payment_activation_authorizations',
                    'payment_activation_authority',
                    'payment_activation_transition_events',
                    'provider_admission_leases'
                ]) AS table_name
                """
            )
            assert not any(
                row["any_table_privilege"] for row in cur.fetchall()
            )

            cur.execute(
                """
                SELECT
                  pg_catalog.has_function_privilege(
                    'finance_config_runtime',
                    'app_secure.pay24a_transition_activation(uuid,bigint,smallint,text,uuid,boolean,boolean,boolean,boolean,boolean,boolean,boolean,boolean,text)',
                    'EXECUTE'
                  ) AS config_transition,
                  pg_catalog.has_function_privilege(
                    'finance_config_runtime',
                    'app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)',
                    'EXECUTE'
                  ) AS config_admission,
                  pg_catalog.has_function_privilege(
                    'finance_payment_runtime',
                    'app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)',
                    'EXECUTE'
                  ) AS payment_admission,
                  pg_catalog.has_function_privilege(
                    'finance_refund_runtime',
                    'app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)',
                    'EXECUTE'
                  ) AS refund_admission,
                  pg_catalog.has_function_privilege(
                    'lifecycle_maintenance_runtime',
                    'app_secure.pay24a_expire_provider_admissions(uuid,text)',
                    'EXECUTE'
                  ) AS maintenance_expiry,
                  pg_catalog.has_function_privilege(
                    'app_runtime',
                    'app_secure.pay24a_activation_snapshot()',
                    'EXECUTE'
                  ) AS app_snapshot
                """
            )
            matrix = cur.fetchone()
            assert matrix == {
                "config_transition": True,
                "config_admission": False,
                "payment_admission": True,
                "refund_admission": True,
                "maintenance_expiry": True,
                "app_snapshot": False,
            }

    _expect_error(
        app_url,
        "SELECT * FROM finance.payment_activation_authority",
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        app_url,
        "SELECT * FROM app_secure.pay24a_activation_snapshot()",
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        attack_url,
        "SELECT * FROM app_secure.pay24a_activation_snapshot()",
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        config_url,
        "UPDATE finance.payment_activation_authority SET checkout=false",
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        payment_url,
        "SELECT * FROM finance.provider_admission_leases",
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        attack_url,
        _TRANSITION_SQL,
        _transition_params(
            uuid.uuid4(),
            0,
            stage=0,
            egress="blocked",
            organization_id=None,
            checkout=False,
        ),
        sqlstate="42501",
        contains="permission denied",
    )
    _expect_error(
        attack_url,
        _REQUEST_ADMISSION_SQL,
        (11, "checkout", "unauthorized-admission", OPERATION_SHA_A, 300),
        sqlstate="42501",
        contains="permission denied",
        organization_id=ORG,
    )
