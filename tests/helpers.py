import io
import zipfile
from pathlib import Path

import numpy as np
import shapely
from pyogrio import raw


def kml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + body + "</Document></kml>"
    ).encode()


def placemark(geometry_xml: str, name: str = "f", pid: str | None = None, extra: str = "") -> str:
    id_attr = f' id="{pid}"' if pid else ""
    return f"<Placemark{id_attr}><name>{name}</name>{extra}{geometry_xml}</Placemark>"


def kml_polygon(coords) -> str:
    text = " ".join(f"{x},{y}" for x, y in coords)
    return f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{text}</coordinates></LinearRing></outerBoundaryIs></Polygon>"


def kml_line(coords) -> str:
    return "<LineString><coordinates>" + " ".join(f"{x},{y}" for x, y in coords) + "</coordinates></LineString>"


def shapefile_zip(
    tmp_path: Path,
    geoms,
    crs: str | None,
    geometry_type: str = "Polygon",
    fields: dict | None = None,
    drop: tuple[str, ...] = (),
    extra_entries: dict[str, bytes] | None = None,
    name: str = "data",
) -> bytes:
    folder = tmp_path / f"shp_{name}_{len(list(tmp_path.iterdir()))}"
    folder.mkdir()
    fields = fields or {"name": np.array([f"f{i}" for i in range(len(geoms))], dtype=object)}
    raw.write(
        str(folder / f"{name}.shp"),
        shapely.to_wkb(np.array(geoms, dtype=object)),
        list(fields.values()),
        list(fields.keys()),
        driver="ESRI Shapefile",
        geometry_type=geometry_type,
        crs=crs,
        encoding="UTF-8",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.iterdir():
            if f.suffix.lower() not in drop:
                zf.write(f, f"layer/{f.name}")
        for entry, data in (extra_entries or {}).items():
            zf.writestr(entry, data)
    return buf.getvalue()


def upload(client, filename: str, data: bytes):
    return client.post("/api/files/", files={"file": (filename, data, "application/octet-stream")})
