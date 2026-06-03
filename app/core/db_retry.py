"""
Database Concurrency Retry Logic: Exponential Backoff for Hyperscale

CRITICAL SQLSTATE Handling:
===========================
- SQLSTATE 40001: Serialization failure (transaction conflict)
- SQLSTATE 40P01: Deadlock detected
- SQLSTATE 55P03: Lock timeout (cannot acquire lock)

All write operations to branch_contacts MUST use this retry wrapper.
Implements exponential backoff with jitter to prevent thundering herd.
Circuit-breaker pattern prevents cascading failures.
"""

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar, Any, Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Enum):
    """Errors that should trigger retry logic"""
    SERIALIZATION_FAILURE = "40001"  # Transaction conflict
    DEADLOCK = "40P01"  # Deadlock detected
    LOCK_TIMEOUT = "55P03"  # Cannot acquire lock within timeout
    TRANSACTION_ABORT = "25P02"  # Transaction aborted (can retry)


class CircuitState(Enum):
    """Circuit breaker state machine"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Too many failures, reject new requests
    HALF_OPEN = "half_open"  # Test if service recovered


class CircuitBreaker:
    """
    Circuit breaker for database operations.
    
    Prevents cascading failures by temporarily rejecting requests
    when error rate exceeds threshold.
    
    State transitions:
    - CLOSED -> OPEN: When failure_count exceeds max_failures
    - OPEN -> HALF_OPEN: After timeout_seconds elapse
    - HALF_OPEN -> CLOSED: After successful test request
    - HALF_OPEN -> OPEN: After failed test request
    """
    
    def __init__(
        self,
        max_failures: int = 5,
        timeout_seconds: int = 60,
        name: str = "db_operations"
    ):
        self.max_failures = max_failures
        self.timeout_seconds = timeout_seconds
        self.name = name
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[datetime] = None
    
    def record_success(self):
        """Record successful operation"""
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"Circuit breaker {self.name} recovered to CLOSED")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
    
    def record_failure(self):
        """Record failed operation"""
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        
        if self.failure_count >= self.max_failures:
            self.state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker {self.name} opened after {self.failure_count} failures"
            )
    
    def check(self) -> bool:
        """
        Check if operation should be allowed.
        
        Returns:
            True if operation allowed, False if circuit is open
        """
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if timeout elapsed
            time_since_failure = (
                datetime.utcnow() - self.last_failure_time
            ).total_seconds()
            
            if time_since_failure >= self.timeout_seconds:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit breaker {self.name} entering HALF_OPEN state")
                return True
            
            return False
        
        # HALF_OPEN: allow single test request
        return True
    
    def is_open(self) -> bool:
        """Check if circuit is currently open"""
        return self.state == CircuitState.OPEN


class ExponentialBackoff:
    """
    Exponential backoff with jitter.
    
    Formula:
        delay = min(base_delay * (multiplier ^ attempt), max_delay) + random_jitter
    
    Prevents thundering herd and distributes retry load.
    """
    
    def __init__(
        self,
        base_delay_ms: int = 100,
        max_delay_ms: int = 10000,
        multiplier: float = 2.0,
        jitter_factor: float = 0.1
    ):
        self.base_delay_ms = base_delay_ms
        self.max_delay_ms = max_delay_ms
        self.multiplier = multiplier
        self.jitter_factor = jitter_factor
    
    def get_delay(self, attempt: int) -> float:
        """
        Get delay for attempt (0-indexed).
        
        Args:
            attempt: Attempt number (0, 1, 2, ...)
        
        Returns:
            Delay in seconds
        """
        exponential_delay = min(
            self.base_delay_ms * (self.multiplier ** attempt),
            self.max_delay_ms
        )
        
        # Add random jitter (±10% of delay)
        jitter = exponential_delay * self.jitter_factor * random.random()
        
        total_delay_ms = exponential_delay + jitter
        return total_delay_ms / 1000.0


def is_retryable_error(exc: Exception) -> bool:
    """
    Determine if error is retryable.
    
    Retryable errors:
    - Serialization failures (transaction conflicts)
    - Deadlocks
    - Lock timeouts
    - Transaction aborts
    
    Non-retryable:
    - Foreign key violations
    - Check constraint violations
    - Unique constraint violations
    """
    if isinstance(exc, IntegrityError):
        # Check constraint or unique constraint - NOT retryable
        return False
    
    if isinstance(exc, DBAPIError):
        # Extract SQLSTATE
        sqlstate = getattr(exc.orig, "sqlstate", None)
        
        if sqlstate in [e.value for e in RetryableError]:
            return True
    
    return False


async def retry_on_db_error(
    func: Callable[..., T],
    *args,
    max_attempts: int = 3,
    backoff: Optional[ExponentialBackoff] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    **kwargs
) -> T:
    """
    Retry async function with exponential backoff on database errors.
    
    Args:
        func: Async function to call
        max_attempts: Maximum number of attempts (default: 3)
        backoff: ExponentialBackoff instance (uses defaults if None)
        circuit_breaker: CircuitBreaker instance (optional)
        *args, **kwargs: Arguments to pass to func
    
    Returns:
        Return value from func
    
    Raises:
        Exception: If all retry attempts exhausted or circuit breaker open
    """
    if backoff is None:
        backoff = ExponentialBackoff()
    
    last_exception = None
    
    for attempt in range(max_attempts):
        try:
            # Check circuit breaker before attempting
            if circuit_breaker and not circuit_breaker.check():
                raise RuntimeError(
                    f"Circuit breaker {circuit_breaker.name} is OPEN. "
                    f"Too many failures. Retrying after timeout."
                )
            
            result = await func(*args, **kwargs)
            
            if circuit_breaker:
                circuit_breaker.record_success()
            
            return result
        
        except Exception as exc:
            last_exception = exc
            
            # Non-retryable errors: fail immediately
            if not is_retryable_error(exc):
                logger.error(
                    f"Non-retryable database error: {exc.__class__.__name__}",
                    extra={"sqlstate": getattr(exc.orig, "sqlstate", None)}
                )
                if circuit_breaker:
                    circuit_breaker.record_failure()
                raise
            
            # Retryable error: log and possibly retry
            logger.warning(
                f"Retryable database error (attempt {attempt + 1}/{max_attempts}): "
                f"{exc.__class__.__name__}",
                extra={"sqlstate": getattr(exc.orig, "sqlstate", None)}
            )
            
            if circuit_breaker:
                circuit_breaker.record_failure()
            
            # Last attempt: raise
            if attempt == max_attempts - 1:
                logger.error(
                    f"All {max_attempts} retry attempts exhausted",
                    extra={"exception": exc}
                )
                raise
            
            # Wait before retry
            delay = backoff.get_delay(attempt)
            logger.info(f"Retrying after {delay:.2f}s")
            await asyncio.sleep(delay)
    
    # Should not reach here
    if last_exception:
        raise last_exception


@asynccontextmanager
async def managed_db_write(
    session: AsyncSession,
    max_attempts: int = 3,
    circuit_breaker: Optional[CircuitBreaker] = None
):
    """
    Transaction guard for database write operations.
    
    Usage:
        async with managed_db_write(session) as txn:
            new_contact = await txn.execute(...)
            await txn.commit()
    
    Handles:
    - Automatic rollback on error
    - Circuit breaker integration

    Note:
        Python context managers cannot safely rerun the caller's block after a
        transaction failure. Use retry_on_db_error() around a callable when the
        whole write operation must be retried.
    """
    _ = max_attempts  # Kept for backward-compatible call sites.

    try:
        if circuit_breaker and not circuit_breaker.check():
            raise RuntimeError(
                f"Circuit breaker {circuit_breaker.name} is OPEN"
            )

        yield session

        if circuit_breaker:
            circuit_breaker.record_success()

    except Exception as exc:
        await session.rollback()
        if not is_retryable_error(exc):
            logger.error(f"Non-retryable error: {exc}")
        else:
            logger.warning(f"Retryable error rolled back: {exc}")

        if circuit_breaker:
            circuit_breaker.record_failure()

        raise


async def execute_managed_db_write(
    operation: Callable[[AsyncSession], Awaitable[T]],
    session: AsyncSession,
    *,
    max_attempts: int = 3,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> T:
    """
    Execute and retry a full write operation.

    The operation callable receives the session and must include all mutations
    that need to be replayed after serialization failures, deadlocks, or lock
    timeouts.
    """

    async def _run_once() -> T:
        async with managed_db_write(session, circuit_breaker=circuit_breaker):
            return await operation(session)

    return await retry_on_db_error(
        _run_once,
        max_attempts=max_attempts,
        circuit_breaker=circuit_breaker,
    )


# ============================================================================
# MONITORING & OBSERVABILITY INSTRUMENTATION
# ============================================================================

class DBOperationMetrics:
    """Track database operation metrics for observability"""
    
    def __init__(self):
        self.total_attempts = 0
        self.successful_attempts = 0
        self.failed_attempts = 0
        self.retry_count = 0
        self.circuit_breaker_rejections = 0
    
    def record_attempt(self, success: bool, retried: bool = False):
        self.total_attempts += 1
        if success:
            self.successful_attempts += 1
        else:
            self.failed_attempts += 1
        if retried:
            self.retry_count += 1
    
    def record_circuit_breaker_rejection(self):
        self.circuit_breaker_rejections += 1
    
    def get_metrics(self) -> dict:
        return {
            "total_attempts": self.total_attempts,
            "successful_attempts": self.successful_attempts,
            "failed_attempts": self.failed_attempts,
            "retry_count": self.retry_count,
            "circuit_breaker_rejections": self.circuit_breaker_rejections,
            "success_rate": (
                self.successful_attempts / self.total_attempts
                if self.total_attempts > 0 else 0
            ),
        }


# Global metrics instance
db_metrics = DBOperationMetrics()
_instrumentation_initialized = False


# ============================================================================
# SQLALCHEMY EVENT LISTENERS FOR INSTRUMENTATION
# ============================================================================

def setup_db_instrumentation():
    """
    Setup SQLAlchemy event listeners for database operation monitoring.
    
    Call this in application startup to enable instrumentation.
    """
    
    global _instrumentation_initialized
    if _instrumentation_initialized:
        return

    @event.listens_for(Session, "after_transaction_create")
    def receive_after_transaction_create(session, transaction):
        transaction._retry_attempt = 0
    
    _instrumentation_initialized = True
    logger.info("Database instrumentation initialized")
