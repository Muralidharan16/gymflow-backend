from sqlalchemy import Column, String, Boolean, Integer, BigInteger, SmallInteger, Text, DateTime, ForeignKey, Date, Numeric, FetchedValue, CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM
from sqlalchemy.orm import relationship
from app.models.base import Base

geo_record_status = ENUM('active', 'deprecated', 'historical', 'pending_validation', name='geo_record_status', create_type=False)
geo_import_status = ENUM('running', 'validating', 'promoted', 'failed', name='geo_import_status', create_type=False)

class Country(Base):
    __tablename__ = "countries"
    
    id = Column(SmallInteger, primary_key=True)
    iso2 = Column(String(2), unique=True, nullable=False)
    iso3 = Column(String(3), unique=True, nullable=False)
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False)
    phone_code = Column(Text)
    currency_code = Column(String(3))
    timezone = Column(Text)
    
    status = Column(geo_record_status, nullable=False, default='active')
    deactivated_at = Column(DateTime(timezone=True))
    deactivation_reason = Column(Text)
    
    postal_code_regex = Column(Text)
    postal_code_min_length = Column(SmallInteger)
    postal_code_max_length = Column(SmallInteger)
    supports_postal_lookup = Column(Boolean, default=True)
    
    ui_config = Column(JSONB)
    source_priority = Column(SmallInteger, default=100)
    confidence_score = Column(SmallInteger, default=100)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())

    subdivisions = relationship("Subdivision", back_populates="country", cascade="none")


class Subdivision(Base):
    __tablename__ = "subdivisions"
    
    id = Column(BigInteger, primary_key=True)
    country_id = Column(SmallInteger, ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False)
    parent_id = Column(BigInteger, ForeignKey("subdivisions.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"))
    code = Column(Text)
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False)
    type = Column(Text, nullable=False)
    timezone = Column(Text)
    
    valid_from = Column(Date)
    valid_until = Column(Date)
    status = Column(geo_record_status, nullable=False, default='active')
    deactivated_at = Column(DateTime(timezone=True))
    deactivation_reason = Column(Text)
    
    source_priority = Column(SmallInteger, default=100)
    confidence_score = Column(SmallInteger, default=100)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
    
    country = relationship("Country", back_populates="subdivisions")
    cities = relationship("City", back_populates="subdivision", cascade="none")


class City(Base):
    __tablename__ = "cities"
    
    id = Column(BigInteger, primary_key=True)
    country_id = Column(SmallInteger, ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False)
    subdivision_id = Column(BigInteger, ForeignKey("subdivisions.id", ondelete="RESTRICT"), nullable=False)
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False)
    latitude = Column(Numeric(9, 6))
    longitude = Column(Numeric(9, 6))
    timezone = Column(Text)
    
    status = Column(geo_record_status, nullable=False, default='active')
    valid_from = Column(Date)
    valid_until = Column(Date)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())

    subdivision = relationship("Subdivision", back_populates="cities")


class PostalCode(Base):
    __tablename__ = "postal_codes"
    
    id = Column(BigInteger, primary_key=True)
    country_id = Column(SmallInteger, ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False)
    subdivision_id = Column(BigInteger, ForeignKey("subdivisions.id", ondelete="RESTRICT"), nullable=False)
    city_id = Column(BigInteger, ForeignKey("cities.id", ondelete="RESTRICT"), nullable=False)
    
    postal_code = Column(Text, nullable=False)
    locality = Column(Text)
    locality_normalized = Column(Text, server_default=FetchedValue())
    latitude = Column(Numeric(9, 6))
    longitude = Column(Numeric(9, 6))
    timezone = Column(Text)
    
    status = Column(geo_record_status, nullable=False, default='active')
    source_priority = Column(SmallInteger, default=100)
    imported_at = Column(DateTime(timezone=True))
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=FetchedValue())
