"""
app/core/drain.py
==================
Kubernetes preStop drain coordination for the Doers SaaS platform.

Ensures zero-downtime rollouts by:
  1. Catching the preStop hook request at `/_system/preStop`.
  2. Marking the pod status as DRAINING.
  3. Intentionally failing `/health` probes (readiness checks) so the ingress/load balancer
     stops routing new requests to this pod.
  4. Waiting for a configurable drain window (e.g. 15s) to allow inflight requests to complete.
  5. Returning success so Kubernetes can proceed with sending SIGTERM to the process.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("doers.drain")


class PodDrainCoordinator:
    """
    Coordinating pre-shutdown drain sequence for zero-downtime deploys.
    """

    def __init__(self, drain_window_seconds: float = 15.0):
        self._drain_window = drain_window_seconds
        self._status = "HEALTHY"  # HEALTHY | DRAINING | SHUTDOWN
        self._inflight_requests = 0
        self._lock = asyncio.Lock()

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_healthy(self) -> bool:
        return self._status == "HEALTHY"

    @property
    def inflight_count(self) -> int:
        return self._inflight_requests

    async def increment_inflight(self):
        async with self._lock:
            self._inflight_requests += 1

    async def decrement_inflight(self):
        async with self._lock:
            self._inflight_requests = max(0, self._inflight_requests - 1)

    async def trigger_drain(self) -> None:
        """
        Triggers the draining state, waits for the load balancer to de-register
        the pod, and monitors remaining inflight requests.
        """
        async with self._lock:
            if self._status == "DRAINING":
                logger.warning("Pod drain already in progress.")
                return
            self._status = "DRAINING"

        logger.info(
            "K8s preStop hook triggered. Entering DRAINING state (drain_window=%.1fs)...",
            self._drain_window,
        )

        # Wait for the load balancer to de-register the pod after health checks fail
        await asyncio.sleep(self._drain_window)

        # Keep checking until inflight requests reach zero or hard timeout is reached
        max_wait = 30.0  # seconds
        wait_interval = 0.5
        elapsed = 0.0

        while self._inflight_requests > 0 and elapsed < max_wait:
            logger.info("Draining... %d inflight requests remain.", self._inflight_requests)
            await asyncio.sleep(wait_interval)
            elapsed += wait_interval

        async with self._lock:
            self._status = "SHUTDOWN"

        logger.info("Drain complete. Ready for process shutdown.")


# Singleton coordinator
drain_coordinator = PodDrainCoordinator()
