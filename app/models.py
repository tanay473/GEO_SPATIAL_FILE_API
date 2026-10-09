from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FileStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FileType(StrEnum):
    SHAPEFILE = "SHAPEFILE"
    KML = "KML"


class MeasurementStatus(StrEnum):
    MEASURED = "MEASURED"
    PARTIAL = "PARTIAL"  # geometry collection: only some parts are measurable
    NOT_APPLICABLE = "NOT_APPLICABLE"  # points
    UNSUPPORTED = "UNSUPPORTED"  # unreadable / exotic geometry type, empty geometry
    FAILED = "FAILED"  # e.g. unknown CRS


def _now() -> datetime:
    return datetime.now(UTC)


class GeoFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[FileType] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[FileStatus] = mapped_column(String(16), default=FileStatus.PENDING)
    storage_path: Mapped[str] = mapped_column(Text)  # GDAL path (may be /vsizip/...)

    crs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crs_is_geographic: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geometry_types: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    layers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pre-aggregated so summaries are a single-row read.
    total_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Feature(Base):
    """One row per feature: geometry, attributes and measurement together, so reads need no joins."""

    __tablename__ = "features"

    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    feature_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    layer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geometry_type: Mapped[str] = mapped_column(String(32))
    geometry: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # 2D WKB, source CRS
    crs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    properties: Mapped[str] = mapped_column(Text, default="{}")  # pre-serialized JSON

    status: Mapped[MeasurementStatus] = mapped_column(String(16))
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    perimeter_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    measurement_crs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
