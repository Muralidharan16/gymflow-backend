import uuid
import enum
import os
from typing import Optional, Any, Dict
from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy import String, Boolean, ForeignKey, text, Text, Integer, event, update, inspect, insert, BigInteger, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, Mapper, relationship
from sqlalchemy.engine import Connection
import re
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB, DOUBLE_PRECISION, INET
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm.attributes import get_history
import geoalchemy2

from app.models.base import Base, TimestampMixin, new_uuid
from app.tasks.geocoding import geocode_address_task

class AddressType(str, enum.Enum):
    registered = "registered"
    operational = "operational"
    billing = "billing"

def check_postgis_available() -> bool:
    """
    Resilient check to see if the active PostgreSQL server actually has PostGIS loaded.
    """
    if os.environ.get("DISABLE_POSTGIS") == "true":
        return False
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return False
    db_url = db_url.replace("+asyncpg", "")
    try:
        engine = sa.create_engine(db_url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            res = conn.execute(sa.text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'postgis'")).scalar()
            return res > 0
    except Exception:
        return False

if check_postgis_available():
    coordinate_type = geoalchemy2.Geography(geometry_type="POINT", srid=4326)
else:
    coordinate_type = sa.String(255)


class OrganizationAddress(Base, TimestampMixin):
    __tablename__ = "organization_addresses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_branches.id", ondelete="RESTRICT"), nullable=False)
    
    address_type: Mapped[str] = mapped_column(String(20), default="physical", server_default=text("'physical'"), nullable=False)
    
    dek_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    formatted_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state_province: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    
    google_place_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_exact_location_visible: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    allow_search_indexing: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"), nullable=False)
    
    _reencryption_in_progress: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)
    
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True)

    geolocation_state: Mapped[Optional["BranchGeolocationState"]] = relationship(
        "BranchGeolocationState",
        back_populates="address",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="joined"
    )

    @property
    def label(self) -> Optional[str]:
        return getattr(self, "_label", None)

    @label.setter
    def label(self, value: Optional[str]) -> None:
        self._label = value

    @property
    def is_primary(self) -> bool:
        return getattr(self, "_is_primary", False)

    @is_primary.setter
    def is_primary(self, value: bool) -> None:
        self._is_primary = value

    @property
    def effective_from(self) -> Optional[datetime]:
        return getattr(self, "_effective_from", None)

    @effective_from.setter
    def effective_from(self, value: Optional[datetime]) -> None:
        self._effective_from = value

    @property
    def effective_until(self) -> Optional[datetime]:
        return getattr(self, "_effective_until", None)

    @effective_until.setter
    def effective_until(self, value: Optional[datetime]) -> None:
        self._effective_until = value

    def _get_or_create_geo_state(self) -> "BranchGeolocationState":
        if not self.geolocation_state:
            self.geolocation_state = BranchGeolocationState(
                address_id=self.id,
                org_id=self.org_id,
                validation_status="pending",
                geocode_attempts=0
            )
        return self.geolocation_state

    @property
    def is_verified(self) -> bool:
        return self._get_or_create_geo_state().validation_status == "success"

    @is_verified.setter
    def is_verified(self, value: bool) -> None:
        self._get_or_create_geo_state().validation_status = "success" if value else "pending"

    @property
    def geocoding_failed(self) -> bool:
        return self._get_or_create_geo_state().validation_status == "failed"

    @geocoding_failed.setter
    def geocoding_failed(self, value: bool) -> None:
        self._get_or_create_geo_state().validation_status = "failed" if value else "pending"

    @property
    def maps_verification_status(self) -> str:
        return self._get_or_create_geo_state().validation_status

    @maps_verification_status.setter
    def maps_verification_status(self, value: str) -> None:
        self._get_or_create_geo_state().validation_status = value

    @property
    def maps_retry_count(self) -> int:
        return self._get_or_create_geo_state().geocode_attempts

    @maps_retry_count.setter
    def maps_retry_count(self, value: int) -> None:
        self._get_or_create_geo_state().geocode_attempts = value

    @property
    def maps_next_retry_at(self) -> Optional[datetime]:
        return self._get_or_create_geo_state().next_retry_at

    @maps_next_retry_at.setter
    def maps_next_retry_at(self, value: Optional[datetime]) -> None:
        self._get_or_create_geo_state().next_retry_at = value

    @property
    def maps_last_verified_at(self) -> Optional[datetime]:
        return self._get_or_create_geo_state().geocoded_at

    @maps_last_verified_at.setter
    def maps_last_verified_at(self, value: Optional[datetime]) -> None:
        self._get_or_create_geo_state().geocoded_at = value

    @property
    def maps_verification_source(self) -> Optional[str]:
        return self._get_or_create_geo_state().geocode_provider

    @maps_verification_source.setter
    def maps_verification_source(self, value: Optional[str]) -> None:
        self._get_or_create_geo_state().geocode_provider = value

    @property
    def maps_verification_error(self) -> Optional[str]:
        return getattr(self, "_maps_verification_error", None)

    @maps_verification_error.setter
    def maps_verification_error(self, value: Optional[str]) -> None:
        self._maps_verification_error = value

    @property
    def maps_embed_allowed(self) -> bool:
        return getattr(self, "_maps_embed_allowed", True)

    @maps_embed_allowed.setter
    def maps_embed_allowed(self, value: bool) -> None:
        self._maps_embed_allowed = value

    @property
    def latitude(self) -> Optional[float]:
        geo = self._get_or_create_geo_state().coordinates
        if geo is not None:
            if isinstance(geo, str):
                m = re.match(r"POINT\(([-\d\.]+) ([\d\.-]+)\)", geo)
                if m:
                    return float(m.group(2))
            return getattr(self, "_latitude", None)
        return getattr(self, "_latitude", None)

    @latitude.setter
    def latitude(self, value: Optional[float]) -> None:
        self._latitude = value
        self._update_coordinates()

    @property
    def longitude(self) -> Optional[float]:
        geo = self._get_or_create_geo_state().coordinates
        if geo is not None:
            if isinstance(geo, str):
                m = re.match(r"POINT\(([-\d\.]+) ([\d\.-]+)\)", geo)
                if m:
                    return float(m.group(1))
            return getattr(self, "_longitude", None)
        return getattr(self, "_longitude", None)

    @longitude.setter
    def longitude(self, value: Optional[float]) -> None:
        self._longitude = value
        self._update_coordinates()

    def _update_coordinates(self) -> None:
        lat = getattr(self, "_latitude", None)
        lng = getattr(self, "_longitude", None)
        if lat is not None and lng is not None:
            point_str = f"POINT({lng} {lat})"
            geo_state = self._get_or_create_geo_state()
            geo_state.coordinates = point_str

    __table_args__ = (
        CheckConstraint("address_type IN ('physical', 'mailing', 'billing', 'registered')", name="chk_address_type"),
        CheckConstraint("address_line1 LIKE 'enc:%'", name="chk_address_line1_encrypted"),
    )


class BranchGeolocationState(Base):
    __tablename__ = "branch_geolocation_state"

    address_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_addresses.id", ondelete="CASCADE"), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)

    address: Mapped["OrganizationAddress"] = relationship(
        "OrganizationAddress",
        back_populates="geolocation_state"
    )
    
    coordinates: Mapped[Optional[Any]] = mapped_column(coordinate_type, nullable=True)
    last_known_good_coordinates: Mapped[Optional[Any]] = mapped_column(coordinate_type, nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    
    validation_status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), nullable=False)
    geocode_version: Mapped[int] = mapped_column(BigInteger, default=1, server_default=text("1"), nullable=False)
    geocode_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    last_geocode_attempt_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    geocoded_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    
    geocode_provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        CheckConstraint("validation_status IN ('pending', 'queued', 'success', 'failed', 'skipped')", name="chk_geocode_status"),
    )


class BranchGeocodeAttempt(Base):
    __tablename__ = "branch_geocode_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_addresses.id", ondelete="CASCADE"), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    geocode_provider: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_response: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("clock_timestamp()"), server_default=text("clock_timestamp()"), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("FALSE"), nullable=False)


class BranchAddressHistory(Base):
    __tablename__ = "branch_address_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    
    dek_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state_province: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    formatted_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    valid_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    valid_to: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("now()"), server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint("address_line1 LIKE 'enc:%'", name="chk_hist_address_line1_encrypted"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="chk_valid_range_nonempty"),
    )


def receive_after_update(mapper, connection, target) -> None:
    from app.tasks.geocoding import geocode_address_task
    connection.execute(None)
    connection.execute(None)
    geocode_address_task.delay(str(target.id))


class BranchAddressAuditLog(Base):
    __tablename__ = "branch_address_audit_log"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    address_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dek_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    old_address: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    new_address: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("now()"), server_default=text("now()"), nullable=False)


# Alias for backward compatibility / tests
AddressAuditLog = BranchAddressAuditLog


class AddressChangeOutbox(Base):
    __tablename__ = "address_change_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("clock_timestamp()"), server_default=text("clock_timestamp()"), nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class MemberAddress(Base, TimestampMixin):
    __tablename__ = "member_addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    address_type: Mapped[AddressType] = mapped_column(
        SAEnum(AddressType, native_enum=False),
        nullable=False,
        default=AddressType.operational,
    )
    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state_province: Mapped[str] = mapped_column(String(100), nullable=False)
    postal_code: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    geocoding_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    coordinates: Mapped[Optional[Any]] = mapped_column(
        coordinate_type, nullable=True
    )
    formatted_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

class GooglePlacesCache(Base):
    __tablename__ = "google_places_cache"

    place_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    latitude: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)
    longitude: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)
    formatted_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    place_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    place_types: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("now()"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=text("now()"), nullable=False)

