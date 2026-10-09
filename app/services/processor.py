"""File-processing pipeline: read -> validate limits -> measure -> persist (one transaction)."""

import io
import logging
import time
from collections import Counter
from datetime import UTC, datetime

import numpy as np
import shapely
from sqlalchemy import delete

from app.config import Settings
from app.db import SessionLocal
from app.errors import ApiError
from app.models import Feature, FileStatus, FileType, GeoFile
from app.services import crs as crs_mod
from app.services.measure import measure_layer
from app.services.reader import read_dataset

log = logging.getLogger(__name__)
INSERT_BATCH = 5000


def _nullable(values: np.ndarray) -> list[float | None]:
    out = values.astype(object)
    out[np.isnan(values)] = None
    return out.tolist()


def _build(file: GeoFile, settings: Settings) -> tuple[list[dict], dict]:
    dataset = read_dataset(file.storage_path, file.file_type == FileType.KML, settings.max_property_chars)
    total = dataset.feature_count
    if total == 0:
        raise ApiError(422, "no_features", "The file contains no features.")
    if total > settings.max_features:
        raise ApiError(413, "too_many_features", f"File has {total} features; the limit is {settings.max_features}.")
    vertices = sum(int(shapely.get_num_coordinates(layer.geometries).sum()) for layer in dataset.layers)
    if vertices > settings.max_vertices:
        raise ApiError(413, "too_many_vertices", f"File has {vertices} vertices; the limit is {settings.max_vertices}.")

    rows: list[dict] = []
    warnings: list[str] = []
    crs_labels: list[str] = []
    geographic: list[bool] = []
    bounds: list[np.ndarray] = []
    index = 0
    total_area = total_length = 0.0
    for layer in dataset.layers:
        m = measure_layer(layer.geometries, layer.unreadable, layer.crs, layer.declared_type)
        label = crs_mod.crs_label(m.resolved_crs)
        if label:
            crs_labels.append(label)
            geographic.append(bool(m.resolved_crs.is_geographic))
        prefix = f"[{layer.name}] " if len(dataset.layers) > 1 else ""
        warnings.extend(prefix + w for w in m.warnings)

        present = ~shapely.is_missing(layer.geometries) & ~shapely.is_empty(layer.geometries)
        if present.any():
            bounds.append(shapely.total_bounds(layer.geometries[present]))

        n = len(layer.geometries)
        columns = {
            "feature_index": range(index, index + n),
            "layer": [layer.name] * n,
            "source_id": layer.source_ids,
            "geometry_type": m.geometry_type.tolist(),
            "geometry": shapely.to_wkb(layer.geometries).tolist(),
            "crs": [label] * n,
            "properties": layer.properties,
            "status": m.status.tolist(),
            "area_m2": _nullable(m.area_m2),
            "length_m": _nullable(m.length_m),
            "perimeter_m": _nullable(m.perimeter_m),
            "measurement_crs": m.measurement_crs.tolist(),
            "note": m.note.tolist(),
        }
        names = list(columns)
        rows.extend({"file_id": file.id, **dict(zip(names, values))} for values in zip(*columns.values()))
        total_area += float(np.nansum(m.area_m2))
        total_length += float(np.nansum(m.length_m))
        index += n

    distinct = list(dict.fromkeys(crs_labels))
    if len(distinct) > 1:
        warnings.append(f"Layers use different CRSs: {', '.join(distinct)}")
    if bounds:
        b = np.vstack(bounds)
        bbox = [float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 2].max()), float(b[:, 3].max())]
    else:
        bbox = None

    summary = {
        "crs": distinct[0] if len(distinct) == 1 else ("MIXED" if distinct else None),
        "crs_is_geographic": geographic[0] if len(set(geographic)) == 1 else None,
        "feature_count": total,
        "geometry_types": dict(Counter(r["geometry_type"] for r in rows)),
        "bbox": bbox,
        "layers": [layer.name for layer in dataset.layers],
        "warnings": warnings,
        "total_area_m2": round(total_area, 3),
        "total_length_m": round(total_length, 3),
        "status_counts": dict(Counter(r["status"] for r in rows)),
    }
    return rows, summary


def process_file(file_id: str, settings: Settings) -> tuple[GeoFile, ApiError | None]:
    """Process a stored upload. Never raises: the file ends up COMPLETED or FAILED.
    Returns the final row plus the client-facing error (if any) for synchronous callers."""
    started = time.perf_counter()
    with SessionLocal() as session:
        file = session.get(GeoFile, file_id)
        file.status = FileStatus.PROCESSING
        session.commit()
        error: ApiError | None = None

        try:
            rows, summary = _build(file, settings)
            session.execute(delete(Feature).where(Feature.file_id == file_id))
            for start in range(0, len(rows), INSERT_BATCH):
                # Core insert: skips ORM per-row bookkeeping (~1.5x faster), still DB-agnostic.
                session.execute(Feature.__table__.insert(), rows[start : start + INSERT_BATCH])
            for key, value in summary.items():
                setattr(file, key, value)
            file.status = FileStatus.COMPLETED
            file.error = None
        except ApiError as exc:
            session.rollback()
            file.status, file.error = FileStatus.FAILED, exc.detail
            error = exc
        except Exception:
            log.exception("Processing failed for file %s", file_id)
            session.rollback()
            file.status, file.error = FileStatus.FAILED, "Internal error while processing the file."
            error = ApiError(500, "processing_error", file.error)

        file.processed_at = datetime.now(UTC)
        file.processing_ms = int((time.perf_counter() - started) * 1000)
        session.commit()
        return file, error


_WARMUP_KML = (
    b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    b"<Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>"
    b"0,0 0.001,0 0.001,0.001 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    b"</Document></kml>"
)


def warm_up() -> None:
    """Pay one-off costs at startup instead of on the first request: GDAL driver
    registration, PROJ database open, GEOS/numpy code paths."""
    from pyogrio import raw

    meta, _, wkb, _ = raw.read(io.BytesIO(_WARMUP_KML), force_2d=True)
    measure_layer(shapely.from_wkb(wkb), np.zeros(len(wkb), dtype=bool), meta["crs"], meta["geometry_type"])
    crs_mod.parse_crs("EPSG:4326")
