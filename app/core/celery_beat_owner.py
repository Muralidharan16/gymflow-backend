"""Redis-backed single-owner scheduler for production Celery Beat."""

from __future__ import annotations

import logging
import math
import os
import socket
import uuid

import redis
from celery.beat import PersistentScheduler

from app.core.config import settings
from app.observability.runtime_metrics import runtime_metrics


_LOGGER = logging.getLogger(__name__)

_ACQUIRE_OR_RENEW_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
    local created = redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2], 'NX')
    if created then
        return 1
    end
    return 0
end
if current == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"production Celery Beat requires {name}")
    return value


def _required_seconds(name: str) -> float:
    raw = _required_env(name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a finite positive number")
    return value


class BeatOwnershipLease:
    """Renewable Redis lease used only for operational scheduler coordination."""

    def __init__(self) -> None:
        if not settings.is_production:
            raise RuntimeError("Doers production Beat ownership is production-only")
        if settings.process_profile != "beat":
            raise RuntimeError(
                "Production Beat scheduler requires DOERS_PROCESS_PROFILE=beat; "
                "embedded worker -B is forbidden"
            )

        self.key = _required_env("CELERY_BEAT_OWNERSHIP_KEY")
        self.ttl_seconds = _required_seconds("CELERY_BEAT_OWNERSHIP_TTL_SECONDS")
        self.retry_seconds = _required_seconds("CELERY_BEAT_OWNERSHIP_RETRY_SECONDS")
        if self.ttl_seconds < 5 or self.ttl_seconds > 300:
            raise RuntimeError("CELERY_BEAT_OWNERSHIP_TTL_SECONDS must be in [5, 300]")
        if self.retry_seconds >= self.ttl_seconds / 2:
            raise RuntimeError(
                "CELERY_BEAT_OWNERSHIP_RETRY_SECONDS must be less than half the lease TTL"
            )

        test_owner = ""
        if os.environ.get("P6S_SCHEDULER_FAULTS") == "1":
            test_owner = str(os.environ.get("P6S_TEST_BEAT_OWNER_ID", "")).strip()
        self.owner_id = test_owner or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        )
        self._ttl_ms = max(1, int(self.ttl_seconds * 1000))
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=min(self.retry_seconds, 2.0),
            socket_timeout=min(self.retry_seconds, 2.0),
            health_check_interval=max(1, int(self.ttl_seconds)),
        )

    def ensure_owned(self) -> bool:
        """Acquire or renew ownership, failing closed on Redis uncertainty."""

        try:
            owned = bool(
                self._client.eval(
                    _ACQUIRE_OR_RENEW_LUA,
                    1,
                    self.key,
                    self.owner_id,
                    self._ttl_ms,
                )
            )
            runtime_metrics().scheduler_state(
                state="owned" if owned else "contended"
            )
            runtime_metrics().redis_state(role="broker", healthy=True)
            return owned
        except (redis.RedisError, OSError):
            runtime_metrics().scheduler_state(state="unavailable")
            runtime_metrics().redis_state(role="broker", healthy=False)
            _LOGGER.exception("Celery Beat ownership check failed closed")
            return False

    def still_owned(self) -> bool:
        """Return True only while Redis names this exact scheduler as owner."""

        try:
            owned = self._client.get(self.key) == self.owner_id
            runtime_metrics().scheduler_state(
                state="owned" if owned else "contended"
            )
            runtime_metrics().redis_state(role="broker", healthy=True)
            return owned
        except (redis.RedisError, OSError):
            runtime_metrics().scheduler_state(state="unavailable")
            runtime_metrics().redis_state(role="broker", healthy=False)
            _LOGGER.exception("Celery Beat ownership verification failed closed")
            return False

    def release(self) -> None:
        """Release only this scheduler's own lease; never delete another owner."""

        try:
            released = bool(self._client.eval(_RELEASE_LUA, 1, self.key, self.owner_id))
            runtime_metrics().scheduler_state(
                state="released" if released else "contended"
            )
        except (redis.RedisError, OSError):
            runtime_metrics().scheduler_state(state="unavailable")
            runtime_metrics().redis_state(role="broker", healthy=False)
            _LOGGER.warning(
                "Celery Beat ownership release could not reach Redis",
                exc_info=True,
            )

    def close(self) -> None:
        self._client.close()


class DoersOwnedPersistentScheduler(PersistentScheduler):
    """PersistentScheduler gated by a renewable, fail-closed Redis owner lease."""

    def __init__(self, *args, **kwargs) -> None:
        self._doers_ownership = BeatOwnershipLease()
        super().__init__(*args, **kwargs)

    @property
    def doers_owner_id(self) -> str:
        return self._doers_ownership.owner_id

    def tick(self, *args, **kwargs):
        if not self._doers_ownership.ensure_owned():
            return self._doers_ownership.retry_seconds

        delay = super().tick(*args, **kwargs)
        if delay is None:
            delay = self.max_interval
        return min(float(delay), self._doers_ownership.retry_seconds)

    def apply_entry(self, entry, producer=None):
        if not self._doers_ownership.ensure_owned():
            _LOGGER.warning(
                "Skipping scheduled task after Beat ownership loss: %s",
                getattr(entry, "name", "<unknown>"),
            )
            return None
        return super().apply_entry(entry, producer=producer)

    def close(self) -> None:
        try:
            self._doers_ownership.release()
            self._doers_ownership.close()
        finally:
            super().close()