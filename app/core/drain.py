"""Graceful API request-drain coordination for DOers P7.

The coordinator owns one process-local admission state.  PostgreSQL remains the
durable business authority; this state only decides whether this API process may
accept a new request during deployment termination.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("doers.drain")


class PodDrainCoordinator:
    """Coordinate request admission and bounded graceful draining."""

    def __init__(
        self,
        drain_window_seconds: float = 15.0,
        *,
        hard_timeout_seconds: float = 30.0,
    ) -> None:
        self._drain_window = max(0.0, float(drain_window_seconds))
        self._hard_timeout = max(0.0, float(hard_timeout_seconds))
        self._status = "HEALTHY"  # HEALTHY | DRAINING | SHUTDOWN
        self._inflight_requests = 0
        self._lock = asyncio.Lock()
        self._zero_inflight = asyncio.Event()
        self._zero_inflight.set()
        self._drain_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_healthy(self) -> bool:
        """Compatibility alias for the old readiness meaning."""
        return self._status == "HEALTHY"

    @property
    def is_ready(self) -> bool:
        return self._status == "HEALTHY"

    @property
    def inflight_count(self) -> int:
        return self._inflight_requests

    async def try_admit_request(self) -> bool:
        """Atomically admit/count a request only while the process is healthy."""
        async with self._lock:
            if self._status != "HEALTHY":
                return False
            self._inflight_requests += 1
            self._zero_inflight.clear()
            return True

    async def release_request(self) -> None:
        """Release one admitted request without ever underflowing the counter."""
        async with self._lock:
            if self._inflight_requests <= 0:
                logger.error("Ignored unbalanced P7 in-flight request release.")
                return
            self._inflight_requests -= 1
            if self._inflight_requests == 0:
                self._zero_inflight.set()

    async def increment_inflight(self) -> None:
        """Legacy compatibility helper; new code must use try_admit_request()."""
        await self.try_admit_request()

    async def decrement_inflight(self) -> None:
        """Legacy compatibility helper; new code must use release_request()."""
        await self.release_request()

    async def _complete_drain(self) -> None:
        logger.info(
            "P7 preStop entered DRAINING (propagation_window=%.1fs, hard_timeout=%.1fs).",
            self._drain_window,
            self._hard_timeout,
        )

        if self._drain_window:
            await asyncio.sleep(self._drain_window)

        try:
            await asyncio.wait_for(
                self._zero_inflight.wait(),
                timeout=self._hard_timeout,
            )
        except TimeoutError:
            logger.warning(
                "P7 drain hard timeout reached with %d admitted request(s) still in flight.",
                self._inflight_requests,
            )

        async with self._lock:
            self._status = "SHUTDOWN"

        logger.info(
            "P7 drain sequence complete (remaining_inflight=%d).",
            self._inflight_requests,
        )

    async def trigger_drain(self) -> None:
        """Begin drain once and await the same bounded drain task on repeat calls.

        The task is shielded from a cancelled HTTP preStop request so readiness
        cannot accidentally return to HEALTHY and later calls do not extend the
        termination budget.
        """
        async with self._lock:
            if self._status == "SHUTDOWN":
                return
            if self._status == "HEALTHY":
                self._status = "DRAINING"
                self._drain_task = asyncio.create_task(self._complete_drain())
            task = self._drain_task

        if task is not None:
            await asyncio.shield(task)


# Singleton coordinator for the API process.
drain_coordinator = PodDrainCoordinator()
