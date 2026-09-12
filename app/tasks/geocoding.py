from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from celery.exceptions import MaxRetriesExceededError

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


class GeocodingAPIError(Exception):
    pass


def _uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _set_sync_tenant_context(db, org_id: uuid.UUID) -> None:
    db.execute(
        sa.text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )


async def _set_async_tenant_context(db, org_id: uuid.UUID) -> None:
    await db.execute(
        sa.text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )


_GEOCODING_INPUT_SQL = sa.text(
    """
    SELECT
        address_data.id,
        address_data.org_id,
        address_data.address_line1,
        address_data.city,
        address_data.state_province,
        address_data.postal_code,
        address_data.country_code,
        address_data.formatted_address,
        address_data.google_place_id,
        COALESCE(geo.validation_status, 'pending') AS validation_status,
        COALESCE(geo.geocode_attempts, 0) AS geocode_attempts,
        geo.next_retry_at
    FROM public.organization_addresses AS address_data
    LEFT JOIN public.branch_geolocation_state AS geo
      ON geo.address_id = address_data.id
     AND geo.org_id = address_data.org_id
    WHERE address_data.id = :address_id
      AND address_data.org_id = :org_id
      AND address_data.deleted_at IS NULL
    """
)


def _load_geocoding_input_sync(db, address_id: uuid.UUID, org_id: uuid.UUID):
    _set_sync_tenant_context(db, org_id)
    return db.execute(
        _GEOCODING_INPUT_SQL,
        {"address_id": address_id, "org_id": org_id},
    ).mappings().one_or_none()


async def _load_geocoding_input_async(db, address_id: uuid.UUID, org_id: uuid.UUID):
    await _set_async_tenant_context(db, org_id)
    result = await db.execute(
        _GEOCODING_INPUT_SQL,
        {"address_id": address_id, "org_id": org_id},
    )
    return result.mappings().one_or_none()


_SUCCESS_ADDRESS_SQL = sa.text(
    """
    UPDATE public.organization_addresses
       SET formatted_address = :formatted_address
     WHERE id = :address_id
       AND org_id = :org_id
    """
)

_SUCCESS_GEO_SQL = sa.text(
    """
    INSERT INTO public.branch_geolocation_state (
        address_id, org_id, coordinates, validation_status, geocode_attempts,
        last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
    ) VALUES (
        :address_id,
        :org_id,
        CASE
            WHEN :latitude IS NULL OR :longitude IS NULL THEN NULL
            ELSE 'POINT(' || CAST(:longitude AS text) || ' ' || CAST(:latitude AS text) || ')'
        END,
        'success', 0, pg_catalog.clock_timestamp(), NULL,
        pg_catalog.clock_timestamp(), :provider
    )
    ON CONFLICT (address_id) DO UPDATE
       SET coordinates = CASE
               WHEN EXCLUDED.coordinates IS NULL
               THEN public.branch_geolocation_state.coordinates
               ELSE EXCLUDED.coordinates
           END,
           validation_status = 'success',
           geocode_attempts = 0,
           last_geocode_attempt_at = pg_catalog.clock_timestamp(),
           next_retry_at = NULL,
           geocoded_at = pg_catalog.clock_timestamp(),
           geocode_provider = EXCLUDED.geocode_provider
     WHERE public.branch_geolocation_state.org_id = EXCLUDED.org_id
    """
)


def _mark_success_sync(
    db,
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    formatted_address: str | None,
    latitude: float | None = None,
    longitude: float | None = None,
    provider: str = "legacy",
) -> None:
    _set_sync_tenant_context(db, org_id)
    db.execute(_SUCCESS_ADDRESS_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "formatted_address": formatted_address,
    })
    db.execute(_SUCCESS_GEO_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "latitude": latitude,
        "longitude": longitude,
        "provider": provider,
    })


async def _mark_success_async(
    db,
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    formatted_address: str | None,
    latitude: float | None,
    longitude: float | None,
    provider: str,
) -> None:
    await _set_async_tenant_context(db, org_id)
    await db.execute(_SUCCESS_ADDRESS_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "formatted_address": formatted_address,
    })
    await db.execute(_SUCCESS_GEO_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "latitude": latitude,
        "longitude": longitude,
        "provider": provider,
    })


_FAILURE_GEO_SQL = sa.text(
    """
    INSERT INTO public.branch_geolocation_state (
        address_id, org_id, validation_status, geocode_attempts,
        last_geocode_attempt_at, next_retry_at, geocode_provider
    ) VALUES (
        :address_id, :org_id, :status, :retry_count,
        pg_catalog.clock_timestamp(), :next_retry_at, :provider
    )
    ON CONFLICT (address_id) DO UPDATE
       SET validation_status = EXCLUDED.validation_status,
           geocode_attempts = EXCLUDED.geocode_attempts,
           last_geocode_attempt_at = pg_catalog.clock_timestamp(),
           next_retry_at = EXCLUDED.next_retry_at,
           geocode_provider = EXCLUDED.geocode_provider
     WHERE public.branch_geolocation_state.org_id = EXCLUDED.org_id
    """
)


def _mark_failure_sync(
    db,
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    retry_count: int,
    permanent: bool,
    next_retry_at: datetime | None,
    error: str,
    notification_message: str,
) -> None:
    _set_sync_tenant_context(db, org_id)
    db.execute(_FAILURE_GEO_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "status": "failed" if permanent else "pending",
        "retry_count": retry_count,
        "next_retry_at": next_retry_at,
        "provider": "google_places_api",
    })
    if permanent:
        lineage_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"maps:fail:{address_id}:{retry_count}")
        db.execute(sa.text(
            "SELECT app_secure.record_org_geocoding_failure("
            ":address_id, :org_id, :error, :retry_count, :message, :lineage_id)"
        ), {
            "address_id": address_id,
            "org_id": org_id,
            "error": error,
            "retry_count": retry_count,
            "message": notification_message,
            "lineage_id": lineage_id,
        })


async def _mark_failure_async(
    db,
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    retry_count: int,
    permanent: bool,
    next_retry_at: datetime | None,
    error: str,
) -> None:
    await _set_async_tenant_context(db, org_id)
    await db.execute(_FAILURE_GEO_SQL, {
        "address_id": address_id,
        "org_id": org_id,
        "status": "failed" if permanent else "pending",
        "retry_count": retry_count,
        "next_retry_at": next_retry_at,
        "provider": "google_places_api",
    })
    if permanent:
        lineage_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"maps:fail:{address_id}:{retry_count}")
        await db.execute(sa.text(
            "SELECT app_secure.record_org_geocoding_failure("
            ":address_id, :org_id, :error, :retry_count, :message, :lineage_id)"
        ), {
            "address_id": address_id,
            "org_id": org_id,
            "error": error,
            "retry_count": retry_count,
            "message": f"Address verification failed permanently with error: {error}",
            "lineage_id": lineage_id,
        })


async def _geocode_address_task_async(address_id: str, org_id: str) -> None:
    from app.core.database import worker_async_session_maker
    from app.services.maps_service import MapsVerificationError, get_coordinates_from_cache_or_api

    address_uuid = _uuid(address_id, field="address_id")
    org_uuid = _uuid(org_id, field="org_id")

    async with worker_async_session_maker() as db:
        row = await _load_geocoding_input_async(db, address_uuid, org_uuid)
        if row is None:
            logger.warning("Tenant-bound geocoding input not visible: address=%s org=%s", address_id, org_id)
            await db.rollback()
            return

        place_id = row["google_place_id"]
        if not place_id:
            await db.rollback()
            return
        if row["next_retry_at"] and row["next_retry_at"] > datetime.now(timezone.utc):
            await db.rollback()
            return

        try:
            data = await get_coordinates_from_cache_or_api(db, place_id)
            if not data:
                raise ValueError(MapsVerificationError.NETWORK_ERROR)
            await _mark_success_async(
                db,
                address_id=address_uuid,
                org_id=org_uuid,
                formatted_address=data.get("formatted_address"),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                provider="google_places_api",
            )
            await db.commit()
            logger.info("Google Place verification succeeded for address=%s", address_id)
        except Exception as exc:
            await db.rollback()
            error = str(exc)
            if error not in MapsVerificationError.ALL:
                error = MapsVerificationError.NETWORK_ERROR
            retry_count = int(row["geocode_attempts"] or 0) + 1
            permanent = retry_count >= 10 or error in {
                MapsVerificationError.PLACE_NOT_FOUND,
                MapsVerificationError.INVALID_PLACE_ID,
                MapsVerificationError.API_DISABLED,
            }
            next_retry_at = None
            if not permanent:
                backoff = 300 * (2 ** (retry_count - 1))
                next_retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=max(30, backoff + random.randint(-30, 30))
                )
            await _mark_failure_async(
                db,
                address_id=address_uuid,
                org_id=org_uuid,
                retry_count=retry_count,
                permanent=permanent,
                next_retry_at=next_retry_at,
                error=error,
            )
            await db.commit()
            logger.warning(
                "Google Place verification failed for address=%s error=%s retry=%d",
                address_id, error, retry_count,
            )


@celery_app.task(
    name="app.tasks.geocoding.geocode_address_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def geocode_address_task(self, address_id: str, org_id: str) -> None:
    """Tenant-bound organization-address geocoding under worker_runtime."""
    from app.core.database import WorkerSyncSessionLocal

    address_uuid = _uuid(address_id, field="address_id")
    org_uuid = _uuid(org_id, field="org_id")
    if not WorkerSyncSessionLocal:
        raise RuntimeError("Worker sync database session is unavailable")

    has_place_id = False
    with WorkerSyncSessionLocal() as db:
        try:
            row = _load_geocoding_input_sync(db, address_uuid, org_uuid)
            if row is None:
                logger.warning("Tenant-bound geocoding input not visible: address=%s org=%s", address_id, org_id)
                db.rollback()
                return

            has_place_id = bool(row["google_place_id"])
            if not has_place_id:
                address_line1 = row["address_line1"] or ""
                if "FAIL" in address_line1:
                    raise GeocodingAPIError("Simulated external Maps API connection failure")
                formatted = ", ".join(
                    str(value)
                    for value in (
                        row["address_line1"], row["city"], row["state_province"],
                        row["postal_code"], row["country_code"],
                    )
                    if value
                )
                _mark_success_sync(
                    db,
                    address_id=address_uuid,
                    org_id=org_uuid,
                    formatted_address=formatted,
                )
                db.commit()
        except Exception as exc:
            if has_place_id:
                raise
            logger.warning("Geocoding attempt failed: %s", exc)
            db.rollback()
            try:
                self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
            except MaxRetriesExceededError as max_exc:
                logger.error("Max retries exhausted for address %s", address_id)
                _mark_failure_sync(
                    db,
                    address_id=address_uuid,
                    org_id=org_uuid,
                    retry_count=int(self.request.retries) + 1,
                    permanent=True,
                    next_retry_at=None,
                    error="MAX_RETRIES_EXCEEDED",
                    notification_message="Your address could not be verified. Please review and re-save.",
                )
                db.commit()
                raise max_exc

    if has_place_id:
        try:
            asyncio.run(_geocode_address_task_async(address_id, org_id))
        except Exception:
            logger.exception("Async maps geocoding failed for address=%s", address_id)
            raise
