"""Response models. They drive the OpenAPI docs; hot paths serialize with orjson directly."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.models import FileStatus, FileType, MeasurementStatus


class AreaUnit(StrEnum):
    m2 = "m2"
    ha = "ha"
    km2 = "km2"


class LengthUnit(StrEnum):
    m = "m"
    km = "km"


AREA_FACTORS = {AreaUnit.m2: 1.0, AreaUnit.ha: 1e-4, AreaUnit.km2: 1e-6}
LENGTH_FACTORS = {LengthUnit.m: 1.0, LengthUnit.km: 1e-3}


class Summary(BaseModel):
    total_area: float
    area_unit: AreaUnit
    total_length: float
    length_unit: LengthUnit
    measured: int
    partial: int
    not_applicable: int
    unsupported: int
    failed: int


class FileOut(BaseModel):
    id: str
    filename: str
    file_type: FileType
    size_bytes: int
    sha256: str
    status: FileStatus
    crs: str | None
    crs_is_geographic: bool | None
    feature_count: int | None
    geometry_types: dict[str, int] | None
    bbox: list[float] | None
    layers: list[str] | None
    warnings: list[str]
    error: str | None
    created_at: datetime
    processed_at: datetime | None
    processing_ms: int | None
    summary: Summary | None = None


class Measurement(BaseModel):
    area: float | None
    perimeter: float | None
    length: float | None
    area_unit: AreaUnit
    length_unit: LengthUnit


class MeasurementItem(BaseModel):
    feature_id: int
    source_id: str | None
    layer: str | None
    geometry_type: str
    crs: str | None
    measurement: Measurement | None
    measurement_crs: str | None
    status: MeasurementStatus
    note: str | None
    geometry: dict[str, Any] | None = None  # only with include=geometry (GeoJSON, source CRS)
    properties: dict[str, Any] | None = None  # only with include=properties


class MeasurementsPage(BaseModel):
    file_id: str
    crs: str | None
    summary: Summary
    items: list[MeasurementItem]
    next_cursor: str | None


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: Any
    instance: str
