"""CRS helpers: parsing, labels, cached transformers and projected-CRS selection.

Strategy for geographic (lon/lat) data, chosen per feature from its centroid:
  * normal case -> local Transverse Mercator (k=1) whose central meridian is the
    feature's longitude snapped to a 0.5 deg grid. A feature is then never more than
    ~0.75 deg from the central meridian, so scale error stays below ~1e-4 (UTM's fixed
    6 deg zones give up to ~0.1% length / ~0.2% area error at zone edges).
  * wide (> 1 deg) or antimeridian-crossing -> Lambert Azimuthal Equal-Area centred on
    the feature, which keeps areas exact regardless of extent.
Snapping to a grid means nearby features share a CRS and are transformed together in
one vectorized call.
"""

from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError

WGS84 = CRS.from_epsg(4326)
LOCAL_TM_GRID_DEG = 0.5
LOCAL_MAX_EXTENT_DEG = 1.0


@lru_cache(maxsize=256)
def parse_crs(value: str | None) -> CRS | None:
    if not value:
        return None
    try:
        return CRS.from_user_input(value)
    except CRSError:
        return None


def crs_label(crs: CRS | None) -> str | None:
    if crs is None:
        return None
    auth = crs.to_authority()
    return f"{auth[0]}:{auth[1]}" if auth else crs.name


@lru_cache(maxsize=256)
def get_transformer(src: CRS, dst: CRS) -> Transformer:
    # always_xy: coordinates are (lon, lat) / (x, y) regardless of the CRS axis order.
    return Transformer.from_crs(src, dst, always_xy=True)


@lru_cache(maxsize=1024)
def get_projector(proj_string: str) -> Transformer:
    """lon/lat (WGS84 degrees) -> projected metres. Built as a bare PROJ pipeline:
    same result as Transformer.from_crs for a WGS84-based target, ~7x cheaper to create
    since PROJ skips the operation search."""
    return Transformer.from_pipeline(
        f"+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step {proj_string}"
    )


def transform_geoms(geoms: np.ndarray, transformer: Transformer) -> np.ndarray:
    def _fn(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geoms, _fn)


def linear_unit_factor(crs: CRS) -> float:
    """Metres per CRS unit (e.g. 0.3048 for feet)."""
    return float(crs.axis_info[0].unit_conversion_factor) if crs.axis_info else 1.0


def unwrap_antimeridian(geoms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shift negative longitudes by +360 for features spanning > 180 deg, which in
    practice means they cross the antimeridian. Returns (geoms, crossed_mask)."""
    bounds = shapely.bounds(geoms)
    crossed = (bounds[:, 2] - bounds[:, 0]) > 180.0
    if crossed.any():
        geoms = geoms.copy()
        geoms[crossed] = shapely.transform(
            geoms[crossed],
            lambda c: np.column_stack([np.where(c[:, 0] < 0, c[:, 0] + 360.0, c[:, 0]), c[:, 1]]),
        )
    return geoms, crossed


def group_by_target(lonlat_geoms: np.ndarray, crossed: np.ndarray) -> dict[str, np.ndarray]:
    """Map projected-CRS PROJ string -> positions of the (already unwrapped) lon/lat
    geometries to measure in it."""
    bounds = shapely.bounds(lonlat_geoms)
    extent = np.maximum(bounds[:, 2] - bounds[:, 0], bounds[:, 3] - bounds[:, 1])
    centroids = shapely.centroid(lonlat_geoms)
    lon = (shapely.get_x(centroids) + 180.0) % 360.0 - 180.0
    lat = shapely.get_y(centroids)

    groups: dict[str, np.ndarray] = {}
    wide = crossed | (extent > LOCAL_MAX_EXTENT_DEG)
    for i in np.flatnonzero(wide):
        key = f"+proj=laea +lat_0={lat[i]:.2f} +lon_0={lon[i]:.2f} +ellps=WGS84"
        groups[key] = np.append(groups.get(key, np.empty(0, dtype=np.intp)), i)

    local = np.flatnonzero(~wide)
    meridians = np.round(lon[local] / LOCAL_TM_GRID_DEG) * LOCAL_TM_GRID_DEG
    unique, inverse = np.unique(meridians, return_inverse=True)
    for k, lon_0 in enumerate(unique):
        groups[f"+proj=tmerc +lat_0=0 +lon_0={lon_0:.1f} +k=1 +ellps=WGS84"] = local[inverse == k]
    return groups
