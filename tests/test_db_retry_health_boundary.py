import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db_retry import CircuitBreaker, CircuitState, execute_managed_db_write, managed_db_write


class _FakeSession:
    def __init__(self):
        self.rollback_count = 0

    async def rollback(self):
        self.rollback_count += 1


class _SerializationFailure(Exception):
    sqlstate = "40001"


@pytest.mark.asyncio
async def test_managed_write_rolls_back_client_error_without_tripping_db_breaker():
    session = _FakeSession()
    breaker = CircuitBreaker(max_failures=1, timeout_seconds=60, name="client-error")

    with pytest.raises(HTTPException):
        async with managed_db_write(session, circuit_breaker=breaker):
            raise HTTPException(status_code=400, detail="invalid request")

    assert session.rollback_count == 1
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_managed_write_integrity_conflict_does_not_trip_db_breaker():
    session = _FakeSession()
    breaker = CircuitBreaker(max_failures=1, timeout_seconds=60, name="integrity-error")
    conflict = IntegrityError("INSERT", {}, Exception("constraint violation"))

    with pytest.raises(IntegrityError):
        async with managed_db_write(session, circuit_breaker=breaker):
            raise conflict

    assert session.rollback_count == 1
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_execute_managed_write_counts_transient_failure_once_per_attempt():
    session = _FakeSession()
    breaker = CircuitBreaker(max_failures=2, timeout_seconds=60, name="retry")
    attempts = 0

    async def operation(_session):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DBAPIError("UPDATE", {}, _SerializationFailure(), False)
        return "ok"

    result = await execute_managed_db_write(
        operation,
        session,
        max_attempts=2,
        circuit_breaker=breaker,
    )

    assert result == "ok"
    assert attempts == 2
    assert session.rollback_count == 1
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED
