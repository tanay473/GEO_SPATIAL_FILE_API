"""Vectorized measurement of one layer.

Area/length are never computed on lon/lat degrees: geographic data is projected
first (see app.services.crs). Already-projected data is measured in its own CRS,
scaled to metres by the CRS's linear unit.

All per-feature work is done with numpy/shapely array operations; Python loops only
touch the rare cases (geometry collections, repaired or antimeridian features).
"""

from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import CRS

from app.models import MeasurementStatus as MS
from app.services import crs as crs_mod

TYPE_NAMES = np.array(
    ["Point", "LineString", "LinearRing", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection"],
    dtype=object,
)
POINTS = [0, 4]
LINES = [1, 2, 5]
POLYGONS = [3, 6]
COLLECTION = 7


@dataclass
class LayerMeasurements:
    geometry_type: np.ndarray  # object (str)
    status: np.ndarray  # object (str)
    area_m2: np.ndarray  # float, NaN = not measured
    length_m: np.ndarray
    perimeter_m: np.ndarray
    measurement_crs: np.ndarray  # object (str | None)
    note: np.ndarray  # object (str | None)
    resolved_crs: CRS | None
    warnings: list[str]


def _measure_collection(geom) -> tuple[float | None, float | None, float | None, bool]:
    """(area, perimeter, length, had_points) from the parts of a collection."""
    parts = shapely.get_parts(geom)
    ids = shapely.get_type_id(parts)
    polys = parts[np.isin(ids, POLYGONS)]
    lines = parts[np.isin(ids, LINES)]
    area = float(shapely.area(polys).sum()) if len(polys) else None
    perimeter = float(shapely.length(polys).sum()) if len(polys) else None
    length = float(shapely.length(lines).sum()) if len(lines) else None
    return area, perimeter, length, bool(np.isin(ids, POINTS).any())


def _project(geoms: np.ndarray, src: CRS, measurable: np.ndarray, mcrs: np.ndarray, note: np.ndarray) -> tuple[np.ndarray, float]:
    """Project the measurable geometries to metric CRSs. Returns (projected, unit scale)."""
    if src.is_projected:
        mcrs[measurable] = crs_mod.crs_label(src)
        return geoms[measurable], crs_mod.linear_unit_factor(src)

    lonlat = geoms[measurable]
    if not src.equals(crs_mod.WGS84, ignore_axis_order=True):
        lonlat = crs_mod.transform_geoms(lonlat, crs_mod.get_transformer(src, crs_mod.WGS84))
    lonlat, crossed = crs_mod.unwrap_antimeridian(lonlat)

    projected = np.empty(len(lonlat), dtype=object)
    for key, idx in crs_mod.group_by_target(lonlat, crossed).items():
        projected[idx] = crs_mod.transform_geoms(lonlat[idx], crs_mod.get_projector(key))
        mcrs[measurable[idx]] = key
    note[measurable[crossed]] = "Crosses the antimeridian; longitudes unwrapped before projecting"
    return projected, 1.0


def measure_layer(geoms: np.ndarray, unreadable: np.ndarray, crs_str: str | None, declared_type: str) -> LayerMeasurements:
    n = len(geoms)
    warnings: list[str] = []
    missing = shapely.is_missing(geoms)
    type_ids = shapely.get_type_id(geoms)  # -1 for missing
    empty = ~missing & shapely.is_empty(geoms)
    is_point = np.isin(type_ids, POINTS)

    geometry_type = TYPE_NAMES[np.clip(type_ids, 0, None)].copy()
    geometry_type[missing] = "None"
    geometry_type[unreadable] = declared_type

    status = np.full(n, MS.UNSUPPORTED.value, dtype=object)
    note = np.full(n, None, dtype=object)
    note[missing] = "Feature has no geometry"
    note[unreadable] = f"Geometry type '{declared_type}' cannot be parsed for measurement"
    note[empty] = "Geometry is empty"
    points = ~empty & is_point
    status[points] = MS.NOT_APPLICABLE.value
    note[points] = "Points have no area or length"

    area = np.full(n, np.nan)
    length = np.full(n, np.nan)
    perimeter = np.full(n, np.nan)
    mcrs = np.full(n, None, dtype=object)

    def result(src):
        return LayerMeasurements(
            geometry_type, status, np.round(area, 3), np.round(length, 3), np.round(perimeter, 3), mcrs, note, src, warnings
        )

    measurable = np.flatnonzero(~missing & ~empty & ~is_point)

    # ---- resolve CRS ---------------------------------------------------------
    src = crs_mod.parse_crs(crs_str)
    if src is None and not missing.all():
        b = shapely.total_bounds(geoms[~missing])
        if b[0] >= -180 and b[2] <= 180 and b[1] >= -90 and b[3] <= 90:
            src = crs_mod.WGS84
            warnings.append("No CRS found; coordinates fit lon/lat range so EPSG:4326 was assumed")

    if src is None or not (src.is_projected or src.is_geographic):
        if len(measurable):
            warnings.append("Unknown or unsupported CRS; features could not be measured")
            status[measurable] = MS.FAILED.value
            note[measurable] = "Unknown CRS: cannot transform to a projected CRS"
        return result(src)
    if not len(measurable):
        return result(src)

    projected, scale = _project(geoms, src, measurable, mcrs, note)

    # ---- repair invalid polygonal geometry ----------------------------------
    m_types = type_ids[measurable]
    can_have_area = np.isin(m_types, POLYGONS) | (m_types == COLLECTION)
    invalid = np.zeros(len(measurable), dtype=bool)
    invalid[can_have_area] = ~shapely.is_valid(projected[can_have_area])
    if invalid.any():
        projected[invalid] = shapely.make_valid(projected[invalid])
        count = int(invalid.sum())
        warnings.append(f"{count} invalid polygon geometr{'y was' if count == 1 else 'ies were'} repaired with make_valid before measuring")

    # ---- measure (vectorized) -----------------------------------------------
    post_types = shapely.get_type_id(projected)
    lines = np.isin(m_types, LINES)
    polys = np.isin(m_types, POLYGONS) & np.isin(post_types, POLYGONS)

    sel = measurable[lines]
    length[sel] = shapely.length(projected[lines]) * scale
    status[sel] = MS.MEASURED.value

    sel = measurable[polys]
    area[sel] = shapely.area(projected[polys]) * scale * scale
    perimeter[sel] = shapely.length(projected[polys]) * scale
    status[sel] = MS.MEASURED.value

    # ---- collections, and polygons that make_valid turned into collections ---
    for pos in np.flatnonzero(~lines & ~polys):
        i = measurable[pos]
        a, p, ln, had_points = _measure_collection(projected[pos])
        if m_types[pos] in POLYGONS:
            area[i], perimeter[i] = (a or 0.0) * scale * scale, (p or 0.0) * scale
            status[i] = MS.MEASURED.value
        elif a is None and ln is None:
            status[i] = MS.NOT_APPLICABLE.value
            note[i] = "Collection contains only points"
        else:
            if a is not None:
                area[i], perimeter[i] = a * scale * scale, p * scale
            if ln is not None:
                length[i] = ln * scale
            status[i] = MS.PARTIAL.value
            kinds = [k for k, v in (("area", a), ("length", ln)) if v is not None]
            note[i] = f"Geometry collection: measured {' and '.join(kinds)} of supported parts" + (
                "; points ignored" if had_points else ""
            )

    for pos in np.flatnonzero(invalid):
        i = measurable[pos]
        note[i] = "Invalid geometry repaired (make_valid) before measuring" + (f"; {note[i]}" if note[i] else "")

    return result(src)
