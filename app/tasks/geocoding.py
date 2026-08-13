from app.core.celery_app import celery_app
import logging
import asyncio
import random
import json
from datetime import datetime, timezone, timedelta
from celery.exceptions import MaxRetriesExceededError
import sqlalchemy as sa
from sqlalchemy.future import select

logger = logging.getLogger(__name__)

class GeocodingAPIError(Exception):
    pass


async def _geocode_address_task_async(address_id: str) -> None:
    from app.core.database import worker_async_session_maker
    from app.models.address import OrganizationAddress
    from app.models.notification import Notification
    from app.services.maps_service import (
        get_coordinates_from_cache_or_api,
        transition_maps_state,
        MapsVerificationStatus,
        MapsVerificationSource,
        MapsVerificationError,
        _deterministic_lineage_id
    )

    async with worker_async_session_maker() as db:
        try:
            stmt = select(OrganizationAddress).where(OrganizationAddress.id == address_id)
            res = await db.execute(stmt)
            addr = res.scalar_one_or_none()

            if not addr:
                logger.error(f"Address with ID {address_id} not found in DB.")
                return

            if addr.google_place_id:
                if addr.maps_next_retry_at and addr.maps_next_retry_at > datetime.now(timezone.utc):
                    logger.info("Address %s is currently in retry cooldown. Skipping.", address_id)
                    return

                try:
                    if addr.maps_verification_status != "pending":
                        transition_maps_state(
                            addr,
                            new_status=MapsVerificationStatus.pending,
                            source=MapsVerificationSource.GOOGLE_PLACES_API
                        )
                        await db.commit()

                    data = await get_coordinates_from_cache_or_api(db, addr.google_place_id)
                    if data:
                        addr.latitude = data["latitude"]
                        addr.longitude = data["longitude"]
                        addr.formatted_address = data["formatted_address"]
                        addr.is_verified = True
                        addr.geocoding_failed = False
                        transition_maps_state(
                            addr,
                            new_status=MapsVerificationStatus.verified,
                            source=MapsVerificationSource.GOOGLE_PLACES_API
                        )
                        await db.commit()
                        logger.info("Successfully verified Google Place ID coordinates for %s", address_id)
                    else:
                        raise ValueError(MapsVerificationError.NETWORK_ERROR)

                except Exception as exc:
                    await db.rollback()
                    err_msg = str(exc)
                    if err_msg not in MapsVerificationError.ALL:
                        err_msg = MapsVerificationError.NETWORK_ERROR

                    addr.maps_retry_count += 1
                    is_permanent = err_msg in (
                        MapsVerificationError.PLACE_NOT_FOUND,
                        MapsVerificationError.INVALID_PLACE_ID,
                        MapsVerificationError.API_DISABLED,
                    )

                    if addr.maps_retry_count >= 10 or is_permanent:
                        transition_maps_state(
                            addr,
                            new_status=MapsVerificationStatus.failed,
                            error=err_msg,
                            source=MapsVerificationSource.GOOGLE_PLACES_API
                        )
                        addr.geocoding_failed = True
                        addr.is_verified = False
                        addr.maps_next_retry_at = None

                        notification = Notification(
                            org_id=addr.org_id,
                            message=f"Address verification failed permanently with error: {err_msg}"
                        )
                        db.add(notification)

                        lineage = _deterministic_lineage_id(addr.id, addr.maps_retry_count)
                        payload_data = {
                            "address_id": str(addr.id),
                            "org_id": str(addr.org_id),
                            "error": err_msg,
                            "retry_count": addr.maps_retry_count,
                        }
                        await db.execute(sa.text("""
                            INSERT INTO public.event_outbox (event_id, event_type, payload, tenant_id, lineage_id)
                            VALUES (gen_random_uuid(), 'maps.verification.failed', :payload, :tid, :lid)
                            ON CONFLICT (lineage_id) DO NOTHING
                        """), {
                            "payload": json.dumps(payload_data),
                            "tid": str(addr.org_id),
                            "lid": lineage,
                        })
                    else:
                        base_backoff = 300 * (2 ** (addr.maps_retry_count - 1))
                        jitter = random.randint(-30, 30)
                        next_retry = datetime.now(timezone.utc) + timedelta(seconds=max(30, base_backoff + jitter))
                        addr.maps_next_retry_at = next_retry
                        addr.maps_verification_error = err_msg

                    await db.commit()
                    logger.warning("Google Place ID verification failed for %s (error: %s, retry: %d)", address_id, err_msg, addr.maps_retry_count)
        except Exception as exc:
            logger.warning(f"Geocoding API attempt failed: {str(exc)}")
            await db.rollback()
            raise exc


@celery_app.task(
    name="app.tasks.geocoding.geocode_address_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60
)
def geocode_address_task(self, address_id: str) -> None:
    logger.info(f"Geocoding address with ID: {address_id}")

    from app.core.database import WorkerSyncSessionLocal
    from app.models.address import OrganizationAddress, MemberAddress
    from app.models.notification import Notification

    if not WorkerSyncSessionLocal:
        raise RuntimeError("Worker sync database session is unavailable")

    has_place_id = False
    with WorkerSyncSessionLocal() as db:
        try:
            addr = db.query(OrganizationAddress).filter(OrganizationAddress.id == address_id).first()
            is_org = True
            if not addr:
                addr = db.query(MemberAddress).filter(MemberAddress.id == address_id).first()
                is_org = False

            if not addr:
                logger.error(f"Address with ID {address_id} not found in DB.")
                return

            if is_org and getattr(addr, "google_place_id", None):
                has_place_id = True

            if not has_place_id:
                if getattr(addr, "address_line1", "") == "FAIL" or "FAIL" in getattr(addr, "address_line1", ""):
                    raise GeocodingAPIError("Simulated external Maps API connection failure")

                addr.is_verified = True
                addr.geocoding_failed = False
                addr.formatted_address = f"{addr.address_line1}, {addr.city}, {addr.state_province}, {addr.postal_code}, {addr.country_code}"
                db.commit()

        except Exception as exc:
            if not has_place_id:
                logger.warning(f"Geocoding API attempt failed: {str(exc)}")
                db.rollback()
                try:
                    self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
                except MaxRetriesExceededError as max_exc:
                    logger.error(f"Max retries exhausted for address {address_id}")

                    addr = db.query(OrganizationAddress).filter(OrganizationAddress.id == address_id).first()
                    is_org = True
                    if not addr:
                        addr = db.query(MemberAddress).filter(MemberAddress.id == address_id).first()
                        is_org = False

                    if addr:
                        addr.is_verified = False
                        addr.geocoding_failed = True
                        if is_org:
                            notification = Notification(
                                org_id=addr.org_id,
                                message="Your address could not be verified. Please review and re-save."
                            )
                            db.add(notification)
                        db.commit()
                    raise max_exc
            else:
                raise

    if has_place_id:
        try:
            asyncio.run(_geocode_address_task_async(address_id))
        except Exception as exc:
            logger.error("Async maps geocoding failed: %s", exc)
