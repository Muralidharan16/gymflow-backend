import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, SmallInteger, TIMESTAMP, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base

class AuditKeyRegistry(Base):
    __tablename__ = "audit_key_registry"

    key_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    kms_key_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    
    algorithm: Mapped[str] = mapped_column(String(32), server_default=text("'aes-256-gcm'"), default="aes-256-gcm", nullable=False)
    digest_algorithm: Mapped[str] = mapped_column(String(32), server_default=text("'sha-256'"), default="sha-256", nullable=False)
    signature_algorithm: Mapped[str] = mapped_column(String(32), server_default=text("'hmac-sha-256'"), default="hmac-sha-256", nullable=False)
    
    rotation_date: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("clock_timestamp()"), default=datetime.utcnow, nullable=False)
    retirement_date: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True, nullable=False)
