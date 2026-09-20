from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.finance_core.domain.provider_boundary import (
    FinanceProviderOperationError,
    ProviderRefundRequest,
    ProviderRefundResponse,
)
from app.finance_core.services.refund_provider_worker import (
    RefundProviderExecutionProcessor,
)
from tests.test_pay10_refund_financial_finalization_runtime import (
    ADMIN_URL,
    COMMAND_ID,
    EVIDENCE_HASH,
    PAYMENT_ID,
    PROVIDER_CODE,
    PROVIDER_PAYMENT_REF,
    REFUND_ID,
    _reset_state,
)


REFUND_URL = os.environ.get("PAY10_REFUND_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REFUND_URL or not ADMIN_URL,
    reason="PAY-10-E isolated PG16 harness is not configured",
)


def _async_refund_url():
    assert REFUND_URL
    # Keep the SQLAlchemy URL object intact. Converting it with str(url)
    # redacts the password as "***", which would turn this CI-only reduced
    # runtime proof into an authentication failure instead of exercising E.
    return make_url(REFUND_URL).set(
        drivername="postgresql+asyncpg",
    )


def _session_factory():
    engine = create_async_engine(
        _async_refund_url(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


class DurableFakeRefundProvider:
    provider_code = PROVIDER_CODE
    environment = "test"

    def __init__(self, path: Path):
        self._path = path
        with sqlite3.connect(self._path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_calls(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_identity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_effects(
                    request_identity TEXT PRIMARY KEY,
                    provider_refund_ref TEXT NOT NULL,
                    receipt TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _identity(request: ProviderRefundRequest) -> str:
        return f"{request.command_id}:{request.refund_id}"

    @staticmethod
    def _provider_ref(identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return "rfnd_" + digest[:24]

    @staticmethod
    def _receipt(identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return "rf_" + digest[:32]

    async def submit_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse:
        identity = self._identity(request)
        provider_ref = self._provider_ref(identity)
        receipt = self._receipt(identity)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO provider_calls(request_identity) VALUES(?)",
                (identity,),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO provider_effects(
                    request_identity,provider_refund_ref,receipt
                ) VALUES(?,?,?)
                """,
                (identity, provider_ref, receipt),
            )
            row = conn.execute(
                """
                SELECT provider_refund_ref,receipt
                FROM provider_effects
                WHERE request_identity=?
                """,
                (identity,),
            ).fetchone()
            assert row is not None
            provider_ref, receipt = row
        return ProviderRefundResponse(
            provider_code=self.provider_code,
            provider_refund_ref=provider_ref,
            provider_payment_ref=request.provider_payment_ref,
            amount=request.amount,
            currency_code=request.currency_code,
            receipt=receipt,
            status="processed",
        )

    async def fetch_refund(
        self,
        request: ProviderRefundRequest,
        *,
        provider_refund_ref: str,
    ) -> ProviderRefundResponse:
        response = await self.submit_refund(request)
        assert response.provider_refund_ref == provider_refund_ref
        return response


class ErrorRefundProvider(DurableFakeRefundProvider):
    def __init__(
        self,
        path: Path,
        *,
        failure_class: str,
        code: str,
    ):
        super().__init__(path)
        self._failure_class = failure_class
        self._code = code

    async def submit_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO provider_calls(request_identity) VALUES(?)",
                (self._identity(request),),
            )
        raise FinanceProviderOperationError(
            provider_code=self.provider_code,
            operation="submit_refund",
            code=self._code,
            failure_class=self._failure_class,
            message="Synthetic PAY-10-E provider failure.",
        )


def _provider_counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as conn:
        calls = conn.execute(
            "SELECT count(*) FROM provider_calls"
        ).fetchone()[0]
        effects = conn.execute(
            "SELECT count(*) FROM provider_effects"
        ).fetchone()[0]
    return int(calls), int(effects)


def _db_row(sql: str, params=()):
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            return row


def _expire_lease() -> None:
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE finance.refund_execution_commands
                SET leased_until=clock_timestamp()-interval '1 second'
                WHERE command_id=%s
                """,
                (COMMAND_ID,),
            )
        conn.commit()


async def _run_once(
    store: Path,
    worker_id: uuid.UUID,
    *,
    fault_point: str | None = None,
    provider=None,
):
    engine, sessions = _session_factory()
    try:
        selected = provider or DurableFakeRefundProvider(store)

        def resolver(provider_code: str):
            assert provider_code == PROVIDER_CODE
            return selected

        def fault(point, claim):
            del claim
            if fault_point == point:
                os._exit(91)

        processor = RefundProviderExecutionProcessor(
            session_factory=sessions,
            provider_resolver=resolver,
            fault_hook=fault if fault_point else None,
        )
        return await processor.run_once(worker_id=worker_id)
    finally:
        await engine.dispose()


def _child_run(
    store: str,
    worker_id: str,
    fault_point: str,
) -> None:
    asyncio.run(
        _run_once(
            Path(store),
            uuid.UUID(worker_id),
            fault_point=fault_point,
        )
    )


def _crash_once(
    store: Path,
    *,
    worker_id: uuid.UUID,
    fault_point: str,
) -> int:
    process = multiprocessing.get_context("fork").Process(
        target=_child_run,
        args=(str(store), str(worker_id), fault_point),
    )
    process.start()
    process.join(timeout=30)
    assert not process.is_alive()
    assert process.exitcode is not None
    return int(process.exitcode)


def test_pay10e_worker_death_after_claim_reclaims_same_attempt(tmp_path: Path):
    _reset_state()
    store = tmp_path / "provider.sqlite3"
    first_worker = uuid.uuid4()
    second_worker = uuid.uuid4()

    assert _crash_once(
        store,
        worker_id=first_worker,
        fault_point="after_claim_commit",
    ) == 91
    assert _provider_counts(store) == (0, 0)

    before = _db_row(
        """
        SELECT status,attempt_count,lease_fence,request_sha256
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert before[0] == "processing"
    assert before[1] == 1
    assert before[3] is None

    # Duplicate/redelivered work while the original lease is still live
    # cannot claim the command and therefore cannot call the provider.
    blocked = asyncio.run(_run_once(store, second_worker))
    assert blocked.state == "idle"
    assert blocked.provider_called is False
    assert _provider_counts(store) == (0, 0)

    _expire_lease()
    result = asyncio.run(_run_once(store, second_worker))
    assert result.state == "reconciliation_pending"
    assert result.reclaimed_existing_attempt is True
    assert _provider_counts(store) == (1, 1)

    after = _db_row(
        """
        SELECT status,attempt_count,lease_fence,request_sha256,
               provider_refund_ref
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert after[0] == "reconciliation_pending"
    assert after[1] == 1
    assert after[2] > before[2]
    assert after[3] is not None
    assert after[4] is not None


def test_pay10e_provider_success_db_ack_loss_replays_one_effect(
    tmp_path: Path,
):
    _reset_state()
    store = tmp_path / "provider.sqlite3"
    first_worker = uuid.uuid4()
    second_worker = uuid.uuid4()

    assert _crash_once(
        store,
        worker_id=first_worker,
        fault_point="after_provider_effect",
    ) == 91
    assert _provider_counts(store) == (1, 1)

    ambiguous = _db_row(
        """
        SELECT status,attempt_count,request_sha256,
               provider_refund_ref,
               provider_evidence_sha256
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert ambiguous[0] == "processing"
    assert ambiguous[1] == 1
    assert ambiguous[2] is not None
    assert ambiguous[3] is None
    assert ambiguous[4] is None

    _expire_lease()
    result = asyncio.run(_run_once(store, second_worker))
    assert result.state == "reconciliation_pending"
    assert result.reclaimed_existing_attempt is True
    assert _provider_counts(store) == (2, 1)

    recovered = _db_row(
        """
        SELECT
            status,
            attempt_count,
            provider_refund_ref,
            provider_evidence_sha256,
            (
                SELECT count(*)
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=c.command_id
            )
        FROM finance.refund_execution_commands c
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert recovered[0] == "reconciliation_pending"
    assert recovered[1] == 1
    assert recovered[2] is not None
    assert recovered[3] is not None
    assert recovered[4] == 1


def test_pay10e_db_commit_broker_ack_loss_redelivery_is_noop(
    tmp_path: Path,
):
    _reset_state()
    store = tmp_path / "provider.sqlite3"
    first_worker = uuid.uuid4()
    second_worker = uuid.uuid4()

    assert _crash_once(
        store,
        worker_id=first_worker,
        fault_point="after_outcome_commit",
    ) == 91
    assert _provider_counts(store) == (1, 1)

    durable = _db_row(
        """
        SELECT
            status,
            provider_refund_ref,
            provider_evidence_sha256,
            (
                SELECT count(*)
                FROM finance.refund_provider_evidence e
                WHERE e.command_id=c.command_id
            )
        FROM finance.refund_execution_commands c
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert durable[0] == "reconciliation_pending"
    assert durable[1] is not None
    assert durable[2] is not None
    assert durable[3] == 1

    result = asyncio.run(_run_once(store, second_worker))
    assert result.state == "idle"
    assert result.provider_called is False
    assert _provider_counts(store) == (1, 1)


@pytest.mark.parametrize(
    ("failure_class", "expected_state"),
    [
        ("retryable", "retry_pending"),
        ("unknown", "reconciliation_pending"),
        ("final", "dead_lettered"),
    ],
)
def test_pay10e_provider_failure_classification_is_durable(
    tmp_path: Path,
    failure_class: str,
    expected_state: str,
):
    _reset_state()
    store = tmp_path / f"{failure_class}.sqlite3"
    provider = ErrorRefundProvider(
        store,
        failure_class=failure_class,
        code=f"PAY10E_{failure_class.upper()}",
    )

    result = asyncio.run(
        _run_once(
            store,
            uuid.uuid4(),
            provider=provider,
        )
    )
    assert result.state == expected_state
    assert _provider_counts(store) == (1, 0)

    row = _db_row(
        """
        SELECT status,leased_by,leased_until,last_error_code
        FROM finance.refund_execution_commands
        WHERE command_id=%s
        """,
        (COMMAND_ID,),
    )
    assert row[0] == expected_state
    assert row[1] is None
    assert row[2] is None
    assert row[3] == f"pay10e_{failure_class}"


def test_pay10e_never_finalizes_finance_from_worker_provider_success(
    tmp_path: Path,
):
    _reset_state()
    store = tmp_path / "provider.sqlite3"
    result = asyncio.run(_run_once(store, uuid.uuid4()))
    assert result.state == "reconciliation_pending"

    row = _db_row(
        """
        SELECT
            (SELECT status FROM finance.refunds WHERE id=%s),
            (SELECT status FROM finance.payments WHERE id=%s),
            (
                SELECT count(*)
                FROM finance.ledger_entries
                WHERE source_type='refund'
                  AND source_id=%s
            ),
            (
                SELECT count(*)
                FROM finance.outbox_events
                WHERE idempotency_key LIKE %s
            )
        """,
        (
            REFUND_ID,
            PAYMENT_ID,
            REFUND_ID,
            f"pay10/{COMMAND_ID}/%",
        ),
    )
    assert row == ("processing", "captured", 0, 0)
