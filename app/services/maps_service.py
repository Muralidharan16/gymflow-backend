# app/services/maps_service.py
from __future__ import annotations

import enum
import logging
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.redis import redis_client
from app.core.crypto import KMSCircuitBreaker
from app.models.address import GooglePlacesCache, OrganizationAddress

logger = logging.getLogger("doers.maps")

class MapsVerificationStatus(str, enum.Enum):
    pending  = "pending"
    verified = "verified"
    failed   = "failed"
    stale    = "stale"
    disabled = "disabled"

class MapsVerificationSource(str, enum.Enum):
    GOOGLE_PLACES_API = "google_places_api"
    GEOCODING_API     = "geocoding_api"
    MANUAL_OVERRIDE   = "manual_override"
    LEGACY_IMPORT     = "legacy_import"
    CACHE_REHYDRATION = "cache_rehydration"

class MapsVerificationError:
    GOOGLE_TIMEOUT   = "GOOGLE_TIMEOUT"
    GOOGLE_QUOTA     = "GOOGLE_QUOTA_EXCEEDED"
    PLACE_NOT_FOUND  = "PLACE_NOT_FOUND"
    INVALID_PLACE_ID = "INVALID_PLACE_ID"
    NETWORK_ERROR    = "NETWORK_ERROR"
    API_DISABLED     = "API_DISABLED"
    BILLING_ERROR    = "BILLING_ERROR"
    PLACE_REMOVED    = "PLACE_REMOVED"

    ALL = {
        GOOGLE_TIMEOUT, GOOGLE_QUOTA, PLACE_NOT_FOUND,
        INVALID_PLACE_ID, NETWORK_ERROR, API_DISABLED,
        BILLING_ERROR, PLACE_REMOVED,
    }

VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending":  {"verified", "failed"},
    "verified": {"stale", "disabled"},
    "stale":    {"verified", "failed"},
    "failed":   {"pending"},
    "disabled": {"pending"},
}

class InvalidMapsTransitionError(ValueError):
    pass


# Telemetry metrics
try:
    from opentelemetry import metrics as otel_metrics
    _meter = otel_metrics.get_meter("doers.maps")
    
    maps_verify_success  = _meter.create_counter("maps_verify_success_total",
                            description="Successful place verifications")
    maps_verify_failure  = _meter.create_counter("maps_verify_failure_total",
                            description="Failed place verifications")
    maps_cache_l1_hit    = _meter.create_counter("maps_cache_l1_hit_total",
                            description="Redis cache hits")
    maps_cache_l2_hit    = _meter.create_counter("maps_cache_l2_hit_total",
                            description="DB cache hits")
    maps_google_api_call = _meter.create_counter("maps_google_api_calls_total",
                            description="Outbound Google API calls")
    maps_privacy_suppressed = _meter.create_counter("maps_privacy_suppressed_total",
                            description="Responses with maps data suppressed for privacy")
    maps_retry_exhausted = _meter.create_counter("maps_retry_exhausted_total",
                            description="Addresses exceeding max retry budget")
    _METRICS_AVAILABLE = True
except Exception:
    _METRICS_AVAILABLE = False


def _inc(counter, labels=None):
    """Safe metric increment — never fails."""
    if _METRICS_AVAILABLE:
        try:
            counter.add(1, labels or {})
        except Exception:
            pass


# Outbound Circuit Breaker for Google API
_google_maps_breaker = KMSCircuitBreaker(
    provider_name="google_maps_api",
    failure_threshold=5,      # 5 consecutive failures -> OPEN
    recovery_timeout=300.0,   # 5 mins cooldown
)

PLACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-:]{10,300}$")


def generate_maps_url(place_id: str | None, lat: float | None, lng: float | None) -> str | None:
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    if lat is not None and lng is not None:
        return f"https://www.google.com/maps?q={lat},{lng}"
    return None


def serialize_public_maps_data(addr, is_exact_visible: bool) -> dict:
    """Privacy-safe maps serialization for public API responses."""
    if not is_exact_visible:
        _inc(maps_privacy_suppressed)
        return {
            "maps_embed_allowed": False,
            "google_place_id": None,
            "maps_url": None,
            "latitude": round(addr.latitude, 2) if addr.latitude else None,
            "longitude": round(addr.longitude, 2) if addr.longitude else None,
        }
    return {
        "maps_embed_allowed": addr.maps_embed_allowed,
        "google_place_id": addr.google_place_id,
        "maps_url": generate_maps_url(addr.google_place_id, addr.latitude, addr.longitude),
        "latitude": addr.latitude,
        "longitude": addr.longitude,
    }


def apply_search_privacy_filter(query, search_radius_meters: int):
    """Exclude hidden-location addresses from micro-radius searches."""
    if search_radius_meters <= 500:
        query = query.where(OrganizationAddress.is_exact_location_visible == True)
    return query


def transition_maps_state(
    addr: OrganizationAddress,
    *,
    new_status: MapsVerificationStatus,
    error: str | None = None,
    source: MapsVerificationSource | None = None,
    reset_retry: bool = False,
) -> None:
    """
    Centralized, guarded state transition for maps verification.
    Raises InvalidMapsTransitionError on illegal transitions.
    """
    current = addr.maps_verification_status
    target  = new_status.value if hasattr(new_status, 'value') else new_status

    if target not in VALID_TRANSITIONS.get(current, set()):
        raise InvalidMapsTransitionError(
            f"Invalid maps state transition: {current} → {target}"
        )

    addr.maps_verification_status = target

    if target == "verified":
        addr.maps_last_verified_at = datetime.now(timezone.utc)
        addr.maps_verification_error = None
        addr.maps_retry_count = 0
        addr.maps_next_retry_at = None
    elif target == "failed":
        addr.maps_verification_error = error

    if reset_retry:
        addr.maps_retry_count = 0
        addr.maps_next_retry_at = None
        addr.maps_verification_error = None

    if source:
        addr.maps_verification_source = source.value if hasattr(source, 'value') else source


def _deterministic_lineage_id(address_id: uuid.UUID | str, retry_count: int) -> uuid.UUID:
    """
    Deterministic lineage UUID prevents duplicate outbox events.
    Same (address_id, retry_count) always produces same UUID v5.
    """
    name = f"maps:fail:{address_id}:{retry_count}"
    return uuid.uuid5(uuid.NAMESPACE_DNS, name)


async def _do_places_api_call(place_id: str) -> dict:
    """Google Places API call with explicit timeout budget."""
    import httpx

    async with asyncio.timeout(15):  # Hard budget: 15s total
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=5.0)
        ) as client:
            _inc(maps_google_api_call)
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "fields": "geometry,formatted_address,name,types",
                    "key": settings.GOOGLE_MAPS_SERVER_API_KEY,
                },
            )
            resp.raise_for_status()
            return resp.json()


async def get_coordinates_from_cache_or_api(
    db: AsyncSession,
    place_id: str,
) -> dict | None:
    """
    Retrieves coordinates and location details from L1 (Redis) or L2 (DB cache),
    falling back to outbound Google Places details API call.
    """
    if not place_id or not PLACE_ID_PATTERN.match(place_id):
        logger.warning("Invalid Google Place ID format: %s", place_id)
        return None

    cache_key = f"maps:place:{place_id}"

    # 1. L1 cache check
    try:
        cached_l1 = await redis_client.get(cache_key)
        if cached_l1:
            _inc(maps_cache_l1_hit)
            return json.loads(cached_l1)
    except Exception as exc:
        logger.error("Redis get failed: %s", exc)

    # 2. L2 database cache check
    cached_l2 = None
    try:
        result = await db.execute(
            select(GooglePlacesCache).where(GooglePlacesCache.place_id == place_id)
        )
        cached_l2 = result.scalar_one_or_none()
        if cached_l2:
            if cached_l2.expires_at > datetime.now(timezone.utc):
                _inc(maps_cache_l2_hit)
                data = {
                    "latitude": cached_l2.latitude,
                    "longitude": cached_l2.longitude,
                    "formatted_address": cached_l2.formatted_address,
                    "place_name": cached_l2.place_name,
                    "place_types": cached_l2.place_types,
                }
                # Back-fill to L1 cache
                try:
                    await redis_client.setex(cache_key, 3600, json.dumps(data))
                except Exception:
                    pass
                return data
    except Exception as exc:
        logger.error("L2 cache lookup error: %s", exc)

    # 3. API Call
    if not await _google_maps_breaker.allow_request():
        logger.warning("Google Maps API circuit breaker open — serving expired L2 cache if available")
        if cached_l2:
            return {
                "latitude": cached_l2.latitude,
                "longitude": cached_l2.longitude,
                "formatted_address": cached_l2.formatted_address,
                "place_name": cached_l2.place_name,
                "place_types": cached_l2.place_types,
            }
        return None

    try:
        api_data = await _do_places_api_call(place_id)
        
        status = api_data.get("status")
        if status == "ZERO_RESULTS":
            raise ValueError(MapsVerificationError.PLACE_NOT_FOUND)
        elif status == "REQUEST_DENIED":
            raise ValueError(MapsVerificationError.API_DISABLED)
        elif status == "OVER_QUERY_LIMIT":
            raise ValueError(MapsVerificationError.GOOGLE_QUOTA)
        elif status == "INVALID_REQUEST":
            raise ValueError(MapsVerificationError.INVALID_PLACE_ID)
        elif status != "OK":
            raise ValueError(MapsVerificationError.NETWORK_ERROR)

        result = api_data.get("result", {})
        geometry = result.get("geometry", {})
        location = geometry.get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")

        if lat is None or lng is None:
            raise ValueError(MapsVerificationError.PLACE_NOT_FOUND)

        data = {
            "latitude": float(lat),
            "longitude": float(lng),
            "formatted_address": result.get("formatted_address"),
            "place_name": result.get("name"),
            "place_types": result.get("types"),
        }

        # Update/Create L2 cache entry
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        if cached_l2:
            cached_l2.latitude = data["latitude"]
            cached_l2.longitude = data["longitude"]
            cached_l2.formatted_address = data["formatted_address"]
            cached_l2.place_name = data["place_name"]
            cached_l2.place_types = data["place_types"]
            cached_l2.verified_at = datetime.now(timezone.utc)
            cached_l2.expires_at = expires_at
            cached_l2.updated_at = datetime.now(timezone.utc)
        else:
            new_cache = GooglePlacesCache(
                place_id=place_id,
                latitude=data["latitude"],
                longitude=data["longitude"],
                formatted_address=data["formatted_address"],
                place_name=data["place_name"],
                place_types=data["place_types"],
                verified_at=datetime.now(timezone.utc),
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            db.add(new_cache)

        # Write to L1 (Redis)
        try:
            await redis_client.setex(cache_key, 3600, json.dumps(data))
        except Exception:
            pass

        await _google_maps_breaker.record_success()
        _inc(maps_verify_success, {"source": "google_places_api"})
        return data

    except Exception as exc:
        await _google_maps_breaker.record_failure()
        _inc(maps_verify_failure, {"error": str(exc)})
        logger.error("Google Places lookup failed: %s", exc)

        # Handle specific custom exceptions or translate them to code
        err_msg = str(exc)
        if err_msg not in MapsVerificationError.ALL:
            if isinstance(exc, asyncio.TimeoutError):
                err_msg = MapsVerificationError.GOOGLE_TIMEOUT
            else:
                err_msg = MapsVerificationError.NETWORK_ERROR

        # Save verification error status if called by the background worker, but we raise it so caller handles state transition
        # Fallback to expired cache if present
        if cached_l2:
            logger.info("Serving expired L2 cache for place %s due to API lookup failure", place_id)
            return {
                "latitude": cached_l2.latitude,
                "longitude": cached_l2.longitude,
                "formatted_address": cached_l2.formatted_address,
                "place_name": cached_l2.place_name,
                "place_types": cached_l2.place_types,
            }
        raise ValueError(err_msg) from exc
