"""Cross-tenant platform maintenance owned by the isolated maintenance process.

These tasks never receive direct table privileges. They install transaction-local
maintenance context and invoke bounded ``app_secure`` SECURITY DEFINER functions.
Geocoding work itself remains tenant-bound on the ordinary worker queue.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

import sqlalchemy as sa
from celery import shared_task

from app.core.database import maintenance_async_session_maker, update_session_context
from app.core.redis import get_redis_utils
from app.tasks.geocoding import geocode_address_task

logger = logging.getLogger(__name__)

_PLATFORM_MAINTENANCE_CONTEXT = "platform"


async def _prepare_platform_maintenance_session(session) -> None:
    await update_session_context(
        session,
        internal_maintenance=_PLATFORM_MAINTENANCE_CONTEXT,
        role="platform_maintenance",
        trace_id="platform-maintenance",
    )


async def _run_expire_legacy_member_subscriptions() -> int:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            result = await session.scalar(
                sa.text(
                    "SELECT app_secure.expire_legacy_member_subscriptions(500)"
                )
            )
            await session.commit()
            count = int(result or 0)
            if count:
                logger.info("Expired %s legacy member subscriptions", count)
            return count
        except Exception:
            await session.rollback()
            logger.exception("Legacy member-subscription expiry failed")
            raise


async def _run_advance_trial_lifecycles() -> dict[str, int]:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT organization_id, new_status
                        FROM app_secure.advance_trial_lifecycles(500)
                        """
                    )
                )
            ).mappings().all()
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Trial lifecycle maintenance failed")
            raise

    counts = Counter(str(row["new_status"]) for row in rows)
    org_ids = {str(row["organization_id"]) for row in rows}
    if org_ids:
        try:
            redis = get_redis_utils().client
            await redis.delete(*(f"trial:{org_id}" for org_id in sorted(org_ids)))
        except Exception:
            # PostgreSQL is the authority. Trial cache entries have a five-minute
            # TTL, so a cache outage must not roll back or replay an already
            # committed lifecycle transition.
            logger.warning(
                "Trial lifecycle cache invalidation failed for %s organizations; "
                "bounded TTL will expire stale entries",
                len(org_ids),
                exc_info=True,
            )

    result = {
        "soft_locked": int(counts.get("soft_locked", 0)),
        "hard_locked": int(counts.get("hard_locked", 0)),
    }
    if rows:
        logger.info(
            "Advanced %s trial lifecycles (%s soft, %s hard)",
            len(rows),
            result["soft_locked"],
            result["hard_locked"],
        )
    return result


async def _run_reclaim_stale_idempotency() -> int:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            result = await session.scalar(
                sa.text(
                    "SELECT app_secure.reclaim_stale_idempotency_keys(30, 500)"
                )
            )
            await session.commit()
            count = int(result or 0)
            if count:
                logger.info("Reclaimed %s stale idempotency leases", count)
            return count
        except Exception:
            await session.rollback()
            logger.exception("Platform idempotency reclaim failed")
            raise


async def _run_archive_expired_idempotency() -> int:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            result = await session.scalar(
                sa.text(
                    "SELECT app_secure.archive_expired_idempotency_keys(48, 1000)"
                )
            )
            await session.commit()
            count = int(result or 0)
            if count:
                logger.info("Archived %s expired idempotency anchors", count)
            return count
        except Exception:
            await session.rollback()
            logger.exception("Platform idempotency archive failed")
            raise


async def _run_geocoding_reverification() -> int:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT address_id, org_id
                        FROM app_secure.claim_due_geocoding_reverification(50)
                        """
                    )
                )
            ).mappings().all()
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Platform geocoding claim failed")
            raise

    for row in rows:
        geocode_address_task.delay(str(row["address_id"]), str(row["org_id"]))
    if rows:
        logger.info("Dispatched %s geocoding reverification tasks", len(rows))
    return len(rows)


async def _run_places_cache_cleanup() -> int:
    async with maintenance_async_session_maker() as session:
        await _prepare_platform_maintenance_session(session)
        try:
            result = await session.scalar(
                sa.text("SELECT app_secure.cleanup_expired_places_cache(1000)")
            )
            await session.commit()
            count = int(result or 0)
            if count:
                logger.info("Deleted %s expired Google Places cache rows", count)
            return count
        except Exception:
            await session.rollback()
            logger.exception("Platform places cache cleanup failed")
            raise


@shared_task(
    name="app.tasks.platform_maintenance.expire_legacy_member_subscriptions",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def expire_legacy_member_subscriptions() -> int:
    return asyncio.run(_run_expire_legacy_member_subscriptions())


@shared_task(
    name="app.tasks.platform_maintenance.advance_trial_lifecycles",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def advance_trial_lifecycles() -> dict[str, int]:
    return asyncio.run(_run_advance_trial_lifecycles())


@shared_task(
    name="app.tasks.platform_maintenance.reclaim_stale_idempotency",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
def reclaim_stale_idempotency() -> int:
    return asyncio.run(_run_reclaim_stale_idempotency())


@shared_task(
    name="app.tasks.platform_maintenance.archive_expired_idempotency",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def archive_expired_idempotency() -> int:
    return asyncio.run(_run_archive_expired_idempotency())


@shared_task(
    name="app.tasks.platform_maintenance.geocoding_reverification",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
def geocoding_reverification() -> int:
    return asyncio.run(_run_geocoding_reverification())


@shared_task(
    name="app.tasks.platform_maintenance.cleanup_places_cache",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def cleanup_places_cache() -> int:
    return asyncio.run(_run_places_cache_cleanup())
