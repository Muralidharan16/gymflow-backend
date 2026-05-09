import uuid
from datetime import datetime
from sqlalchemy import String, Integer, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP
from sqlalchemy import Enum as SAEnum

from app.models.base import Base, TimestampMixin, new_uuid
from app.models.enums import ImportStatus


class ImportLog(Base, TimestampMixin):
    __tablename__ = "import_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    gym_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gyms.id", ondelete="CASCADE"),
        nullable=False,
    )
    imported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gym_owners.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        SAEnum(ImportStatus, name="importstatus", create_constraint=False),
        default=ImportStatus.processing,
        nullable=False,
    )
    total_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_payload: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    column_mapping: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    source_software: Mapped[str | None] = mapped_column(String(50), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_import_logs_gym_id", "gym_id"),
    )
