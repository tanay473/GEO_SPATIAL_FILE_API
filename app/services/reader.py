"""Read every layer of a GDAL dataset into numpy arrays (no per-feature Python objects
until serialization)."""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time

import numpy as np
import orjson
import pyogrio
import shapely
from pyogrio import raw
from pyogrio.errors import DataSourceError, DataLayerError

from app.errors import ApiError

# Styling fields LIBKML adds to every layer. They are noise unless actually set.
_KML_DEFAULTS = {
    "timestamp": (None,),
    "begin": (None,),
    "end": (None,),
    "altitudeMode": (None,),
    "tessellate": (None, -1),
    "extrude": (None, 0),
    "visibility": (None, -1),
    "drawOrder": (None,),
    "icon": (None,),
}


@dataclass
class Layer:
    name: str
    crs: str | None  # as reported by GDAL ("EPSG:4326", WKT, or None)
    declared_type: str
    source_ids: list[str | None]
    geometries: np.ndarray  # shapely objects, 2D; None where unreadable/null
    unreadable: np.ndarray  # bool: WKB present but shapely could not parse it
    properties: list[str]  # pre-serialized JSON per feature


@dataclass
class Dataset:
    layers: list[Layer] = field(default_factory=list)

    @property
    def feature_count(self) -> int:
        return sum(len(layer.geometries) for layer in self.layers)


def count_features(path: str) -> int:
    """Cheap feature count from layer metadata (no geometry decoding)."""
    try:
        layers = pyogrio.list_layers(path)
        return sum(max(pyogrio.read_info(path, layer=name)["features"], 0) for name in layers[:, 0])
    except (DataSourceError, DataLayerError) as exc:
        raise ApiError(422, "unreadable_file", f"GDAL could not open the file: {exc}") from exc


def _json_value(value, max_chars: int):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…[truncated]"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def _serialize_properties(
    field_names: list[str], columns: list[list], n: int, is_kml: bool, max_chars: int
) -> tuple[list[str | None], list[str]]:
    id_col = field_names.index("id") if is_kml and "id" in field_names else None
    source_ids: list[str | None] = [None] * n
    out: list[str] = []
    for i in range(n):
        props = {}
        for j, name in enumerate(field_names):
            value = columns[j][i]
            if j == id_col:
                source_ids[i] = None if value is None else str(value)
                continue
            if is_kml and name in _KML_DEFAULTS and (
                value in _KML_DEFAULTS[name] or (isinstance(value, float) and math.isnan(value))
            ):
                continue
            props[name] = _json_value(value, max_chars)
        out.append(orjson.dumps(props).decode())
    return source_ids, out


def read_dataset(path: str, is_kml: bool, max_property_chars: int) -> Dataset:
    try:
        layer_names = pyogrio.list_layers(path)[:, 0]
    except (DataSourceError, DataLayerError) as exc:
        raise ApiError(422, "unreadable_file", f"GDAL could not open the file: {exc}") from exc

    dataset = Dataset()
    for name in layer_names:
        try:
            meta, fids, wkb, fields = raw.read(path, layer=name, force_2d=True, return_fids=True)
        except (DataSourceError, DataLayerError) as exc:
            raise ApiError(422, "unreadable_file", f"Failed to read layer {name!r}: {exc}") from exc
        n = len(fids)
        if n == 0:
            continue

        geoms = shapely.from_wkb(wkb, on_invalid="ignore")
        has_wkb = np.array([b is not None for b in wkb], dtype=bool)
        unreadable = has_wkb & shapely.is_missing(geoms)

        source_ids, props = _serialize_properties(
            list(meta["fields"]), [f.tolist() for f in fields], n, is_kml, max_property_chars
        )
        if not is_kml:
            source_ids = [str(fid) for fid in fids.tolist()]

        dataset.layers.append(
            Layer(
                name=str(name),
                crs=meta["crs"],
                declared_type=meta["geometry_type"],
                source_ids=source_ids,
                geometries=geoms,
                unreadable=unreadable,
                properties=props,
            )
        )
    return dataset
