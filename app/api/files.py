import base64
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import orjson
import shapely
from fastapi import APIRouter, BackgroundTasks, Depends, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.config import Settings, get_settings
from app.db import get_session
from app.errors import ApiError
from app.models import Feature, FileStatus, FileType, GeoFile, MeasurementStatus
from app.schemas import (
    AREA_FACTORS,
    LENGTH_FACTORS,
    AreaUnit,
    FileOut,
    LengthUnit,
    MeasurementsPage,
    Problem,
)
from app.services.processor import process_file
from app.services.reader import count_features
from app.services.upload import detect_file_type, save_stream, validate_kml, validate_shapefile_zip

router = APIRouter(prefix="/api/files", tags=["files"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _errors(*codes: int) -> dict:
    return {code: {"model": Problem} for code in codes}


def _json(content, status_code: int = 200, headers: dict | None = None) -> Response:
    return Response(orjson.dumps(content), status_code=status_code, media_type="application/json", headers=headers)


def _convert(value: float | None, factor: float) -> float | None:
    if value is None:
        return None
    return round(value * factor, 3 if factor == 1.0 else 9)


def _summary(file: GeoFile, area_unit: AreaUnit, length_unit: LengthUnit) -> dict:
    counts = file.status_counts or {}
    return {
        "total_area": _convert(file.total_area_m2 or 0.0, AREA_FACTORS[area_unit]),
        "area_unit": area_unit.value,
        "total_length": _convert(file.total_length_m or 0.0, LENGTH_FACTORS[length_unit]),
        "length_unit": length_unit.value,
        **{s.value.lower(): counts.get(s.value, 0) for s in MeasurementStatus},
    }


def _iso(value: datetime | None) -> str | None:
    # SQLite drops tzinfo on read; every timestamp we store is UTC.
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _file_body(file: GeoFile, include_summary: bool = False) -> dict:
    body = {
        "id": file.id,
        "filename": file.filename,
        "file_type": file.file_type,
        "size_bytes": file.size_bytes,
        "sha256": file.sha256,
        "status": file.status,
        "crs": file.crs,
        "crs_is_geographic": file.crs_is_geographic,
        "feature_count": file.feature_count,
        "geometry_types": file.geometry_types,
        "bbox": file.bbox,
        "layers": file.layers,
        "warnings": file.warnings or [],
        "error": file.error,
        "created_at": _iso(file.created_at),
        "processed_at": _iso(file.processed_at),
        "processing_ms": file.processing_ms,
    }
    if include_summary:
        body["summary"] = _summary(file, AreaUnit.m2, LengthUnit.m) if file.status == FileStatus.COMPLETED else None
    return body


def _get_file(session: Session, file_id: str) -> GeoFile:
    file = session.get(GeoFile, file_id)
    if file is None:
        raise ApiError(404, "not_found", f"File {file_id!r} not found.")
    return file


def _encode_cursor(index: int) -> str:
    return base64.urlsafe_b64encode(str(index).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except ValueError as exc:
        raise ApiError(400, "invalid_cursor", "Malformed pagination cursor.") from exc


# --------------------------------------------------------------------------- upload


def _intake(src, file_type: FileType, upload_dir: Path, settings: Settings):
    """Blocking part of the upload: save, validate, count. Runs in the threadpool."""
    saved = save_stream(src, upload_dir / f"source.{'zip' if file_type == FileType.SHAPEFILE else 'kml'}", settings.max_upload_bytes)
    if file_type == FileType.SHAPEFILE:
        gdal_path = validate_shapefile_zip(saved.path, settings)
    else:
        gdal_path = validate_kml(saved.path)
    return saved, gdal_path


@router.post(
    "/",
    status_code=201,
    response_model=FileOut,
    responses={
        200: {"model": FileOut, "description": "Identical file already uploaded; existing record returned."},
        202: {"model": FileOut, "description": "Large file accepted and queued; poll GET /api/files/{id}/."},
        **_errors(400, 413, 415, 422),
    },
    summary="Upload and process a geospatial file (.zip Shapefile or .kml)",
)
async def upload_file(file: UploadFile, background: BackgroundTasks, session: SessionDep, settings: SettingsDep):
    file_type = detect_file_type(file.filename or "")
    file_id = uuid.uuid4().hex
    upload_dir = settings.upload_dir / file_id

    try:
        saved, gdal_path = await run_in_threadpool(_intake, file.file, file_type, upload_dir, settings)

        existing = session.scalars(
            select(GeoFile)
            .where(GeoFile.sha256 == saved.sha256, GeoFile.status != FileStatus.FAILED)
            .order_by(GeoFile.created_at.desc())
            .limit(1)
        ).first()
        if existing is not None:
            shutil.rmtree(upload_dir, ignore_errors=True)
            return _json(_file_body(existing), 200, {"Location": f"/api/files/{existing.id}/"})

        feature_count = await run_in_threadpool(count_features, gdal_path)
    except BaseException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise

    session.add(
        GeoFile(
            id=file_id,
            filename=Path(file.filename).name[:255],
            file_type=file_type,
            size_bytes=saved.size,
            sha256=saved.sha256,
            status=FileStatus.PENDING,
            storage_path=gdal_path,
            warnings=[],
        )
    )
    session.commit()
    location = {"Location": f"/api/files/{file_id}/"}

    if saved.size <= settings.sync_max_bytes and feature_count <= settings.sync_max_features:
        record, error = await run_in_threadpool(process_file, file_id, settings)
        if error is not None:
            session.delete(session.get(GeoFile, file_id))
            session.commit()
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise error
        return _json(_file_body(record), 201, location)

    background.add_task(process_file, file_id, settings)
    return _json(_file_body(session.get(GeoFile, file_id)), 202, location)


# --------------------------------------------------------------------------- file info


@router.get(
    "/{file_id}/",
    response_model=FileOut,
    responses=_errors(404, 422),
    summary="Information about an uploaded file",
)
def get_file(
    file_id: str,
    session: SessionDep,
    include: Annotated[Literal["summary"] | None, Query(description="`summary` adds measurement totals")] = None,
):
    file = _get_file(session, file_id)
    return _json(_file_body(file, include_summary=include == "summary"))


# --------------------------------------------------------------------------- measurements


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsPage,
    responses=_errors(400, 404, 409, 422),
    summary="Per-feature measurements (optionally with geometry and properties)",
)
def get_measurements(
    file_id: str,
    session: SessionDep,
    settings: SettingsDep,
    include: Annotated[str | None, Query(description="Comma-separated: `geometry`, `properties`")] = None,
    feature_id: Annotated[int | None, Query(ge=0, description="Return a single feature")] = None,
    geometry_type: Annotated[str | None, Query(description="e.g. Polygon, LineString")] = None,
    status: MeasurementStatus | None = None,
    layer: str | None = None,
    area_unit: AreaUnit = AreaUnit.m2,
    length_unit: LengthUnit = LengthUnit.m,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
):
    file = _get_file(session, file_id)
    if file.status != FileStatus.COMPLETED:
        detail = f"File is {file.status}." + (f" {file.error}" if file.error else "")
        raise ApiError(409, "file_not_ready", detail)

    includes = {part.strip() for part in (include or "").split(",") if part.strip()}
    unknown = includes - {"geometry", "properties"}
    if unknown:
        raise ApiError(400, "invalid_include", f"Unknown include value(s): {', '.join(sorted(unknown))}")
    page_size = min(limit or settings.default_page_size, settings.max_page_size)

    columns = [
        Feature.feature_index, Feature.source_id, Feature.layer, Feature.geometry_type, Feature.crs,
        Feature.status, Feature.area_m2, Feature.length_m, Feature.perimeter_m,
        Feature.measurement_crs, Feature.note,
    ]
    if "geometry" in includes:
        columns.append(Feature.geometry)
    if "properties" in includes:
        columns.append(Feature.properties)

    query = select(Feature).options(load_only(*columns)).where(Feature.file_id == file_id)
    if feature_id is not None:
        query = query.where(Feature.feature_index == feature_id)
    if geometry_type:
        query = query.where(Feature.geometry_type == geometry_type)
    if status:
        query = query.where(Feature.status == status.value)
    if layer:
        query = query.where(Feature.layer == layer)
    if cursor:
        query = query.where(Feature.feature_index > _decode_cursor(cursor))
    rows = session.scalars(query.order_by(Feature.feature_index).limit(page_size + 1)).all()

    if feature_id is not None and not rows:
        raise ApiError(404, "not_found", f"Feature {feature_id} not found in file {file_id!r}.")

    has_more = len(rows) > page_size
    rows = rows[:page_size]

    geojson = None
    if "geometry" in includes:
        geojson = shapely.to_geojson(shapely.from_wkb([r.geometry for r in rows])) if rows else []

    af, lf = AREA_FACTORS[area_unit], LENGTH_FACTORS[length_unit]
    items = []
    for pos, r in enumerate(rows):
        measured = r.area_m2 is not None or r.length_m is not None
        item = {
            "feature_id": r.feature_index,
            "source_id": r.source_id,
            "layer": r.layer,
            "geometry_type": r.geometry_type,
            "crs": r.crs,
            "measurement": {
                "area": _convert(r.area_m2, af),
                "perimeter": _convert(r.perimeter_m, lf),
                "length": _convert(r.length_m, lf),
                "area_unit": area_unit.value,
                "length_unit": length_unit.value,
            } if measured else None,
            "measurement_crs": r.measurement_crs,
            "status": r.status,
            "note": r.note,
        }
        if geojson is not None:
            # Pre-serialized JSON is embedded as-is: no parse/re-serialize round trip.
            item["geometry"] = orjson.Fragment(geojson[pos]) if geojson[pos] is not None else None
        if "properties" in includes:
            item["properties"] = orjson.Fragment(r.properties)
        items.append(item)

    return _json(
        {
            "file_id": file.id,
            "crs": file.crs,
            "summary": _summary(file, area_unit, length_unit),
            "items": items,
            "next_cursor": _encode_cursor(rows[-1].feature_index) if has_more else None,
        }
    )
