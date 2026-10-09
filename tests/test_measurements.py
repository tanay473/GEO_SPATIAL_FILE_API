"""Measurement correctness: projected results are checked against geodesic truth (pyproj.Geod)."""

import numpy as np
import pytest
import shapely
from pyproj import Geod

from helpers import kml, kml_line, kml_polygon, placemark, shapefile_zip, upload

GEOD = Geod(ellps="WGS84")


def geodesic_area(coords) -> float:
    return abs(GEOD.geometry_area_perimeter(shapely.Polygon(coords))[0])


def geodesic_length(coords) -> float:
    return GEOD.geometry_length(shapely.LineString(coords))


def items(client, file_id, **params):
    r = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_kml_polygon_line_point_against_geodesic(client):
    poly = [(77.50, 12.90), (77.52, 12.90), (77.52, 12.92), (77.50, 12.92), (77.50, 12.90)]
    line = [(77.5, 12.9), (77.6, 13.0), (77.7, 13.0)]
    body = (
        placemark(kml_polygon(poly), "plot", "plot-1",
                  '<ExtendedData><Data name="owner"><value>Ravi</value></Data></ExtendedData>')
        + placemark(kml_line(line), "road")
        + placemark("<Point><coordinates>77.5,12.9,15</coordinates></Point>", "well")
    )
    r = upload(client, "survey.kml", kml(body))
    assert r.status_code == 201, r.text
    info = r.json()
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert info["crs_is_geographic"] is True
    assert info["feature_count"] == 3
    assert info["geometry_types"] == {"Polygon": 1, "LineString": 1, "Point": 1}
    assert r.headers["location"] == f"/api/files/{info['id']}/"

    page = items(client, info["id"], include="properties")
    by_type = {i["geometry_type"]: i for i in page["items"]}

    p = by_type["Polygon"]
    assert p["status"] == "MEASURED"
    assert p["measurement_crs"].startswith("+proj=tmerc +lat_0=0 +lon_0=77.5 ")
    assert p["source_id"] == "plot-1"
    assert p["properties"]["owner"] == "Ravi"
    assert p["measurement"]["area"] == pytest.approx(geodesic_area(poly), rel=1e-4)
    assert p["measurement"]["perimeter"] > 0

    ln = by_type["LineString"]
    assert ln["status"] == "MEASURED"
    assert ln["measurement"]["area"] is None
    assert ln["measurement"]["length"] == pytest.approx(geodesic_length(line), rel=1e-4)

    pt = by_type["Point"]
    assert pt["status"] == "NOT_APPLICABLE"
    assert pt["measurement"] is None

    s = page["summary"]
    assert s["measured"] == 2 and s["not_applicable"] == 1
    assert s["total_area"] == pytest.approx(p["measurement"]["area"])


def test_projected_shapefile_measured_natively(client, tmp_path):
    geoms = [shapely.box(500000, 1400000, 500100, 1400050), shapely.MultiPolygon([shapely.box(0, 0, 10, 10), shapely.box(20, 20, 30, 30)])]
    r = upload(client, "plots.zip", shapefile_zip(tmp_path, geoms, "EPSG:32643"))
    assert r.status_code == 201, r.text
    page = items(client, r.json()["id"])
    a, b = page["items"]
    assert a["measurement"]["area"] == 5000.0
    assert a["measurement"]["perimeter"] == 300.0
    assert a["measurement_crs"] == "EPSG:32643"
    assert b["geometry_type"] == "MultiPolygon"
    assert b["measurement"]["area"] == 200.0


def test_projected_crs_in_feet_converted_to_metres(client, tmp_path):
    # EPSG:2263 = NY State Plane, US survey feet
    r = upload(client, "ny.zip", shapefile_zip(tmp_path, [shapely.box(1000000, 200000, 1000100, 200100)], "EPSG:2263"))
    item = items(client, r.json()["id"])["items"][0]
    ft = 1200 / 3937
    assert item["measurement"]["area"] == pytest.approx(10000 * ft * ft, abs=1e-3)  # API rounds to mm
    assert item["measurement"]["perimeter"] == pytest.approx(400 * ft, abs=1e-3)


def test_missing_prj_lonlat_assumes_wgs84(client, tmp_path):
    poly = [(10, 50), (10.01, 50), (10.01, 50.01), (10, 50.01), (10, 50)]
    data = shapefile_zip(tmp_path, [shapely.Polygon(poly)], "EPSG:4326", drop=(".prj",))
    r = upload(client, "noprj.zip", data)
    info = r.json()
    assert info["crs"] == "EPSG:4326"
    assert any("assumed" in w for w in info["warnings"])
    item = items(client, info["id"])["items"][0]
    assert item["measurement"]["area"] == pytest.approx(geodesic_area(poly), rel=1e-4)


def test_missing_prj_projected_coords_fail_gracefully(client, tmp_path):
    data = shapefile_zip(tmp_path, [shapely.box(500000, 4000000, 500100, 4000100)], "EPSG:32633", drop=(".prj",))
    r = upload(client, "unknown.zip", data)
    assert r.status_code == 201
    item = items(client, r.json()["id"])["items"][0]
    assert item["status"] == "FAILED"
    assert "Unknown CRS" in item["note"]


def test_invalid_bowtie_polygon_is_repaired(client):
    bowtie = [(0, 0), (0.01, 0.01), (0.01, 0), (0, 0.01), (0, 0)]
    r = upload(client, "bowtie.kml", kml(placemark(kml_polygon(bowtie))))
    info = r.json()
    assert any("make_valid" in w for w in info["warnings"])
    item = items(client, info["id"])["items"][0]
    assert item["status"] == "MEASURED"
    assert "repaired" in item["note"]
    # two triangles, each a quarter of the 0.01-degree square
    square = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01), (0, 0)]
    assert item["measurement"]["area"] == pytest.approx(geodesic_area(square) / 2, rel=2e-3)


def test_antimeridian_polygon(client):
    coords = [(179.9, -17.0), (-179.9, -17.0), (-179.9, -16.9), (179.9, -16.9), (179.9, -17.0)]
    r = upload(client, "fiji.kml", kml(placemark(kml_polygon(coords))))
    item = items(client, r.json()["id"])["items"][0]
    unwrapped = [(x + 360 if x < 0 else x, y) for x, y in coords]
    assert item["status"] == "MEASURED"
    assert "+proj=laea" in item["measurement_crs"]
    assert "antimeridian" in item["note"]
    assert item["measurement"]["area"] == pytest.approx(geodesic_area(unwrapped), rel=1e-3)


def test_polar_polygon(client):
    coords = [(10, 85), (10.5, 85), (10.5, 85.1), (10, 85.1), (10, 85)]
    r = upload(client, "arctic.kml", kml(placemark(kml_polygon(coords))))
    item = items(client, r.json()["id"])["items"][0]
    assert item["measurement_crs"].startswith("+proj=tmerc")
    assert item["measurement"]["area"] == pytest.approx(geodesic_area(coords), rel=1e-4)


def test_wide_polygon_uses_equal_area(client):
    coords = [(0, 40), (20, 40), (20, 50), (0, 50), (0, 40)]
    r = upload(client, "wide.kml", kml(placemark(kml_polygon(coords))))
    item = items(client, r.json()["id"])["items"][0]
    assert "+proj=laea" in item["measurement_crs"]
    # Geod follows geodesic edges, LAEA keeps edges straight in projection; close but not identical.
    assert item["measurement"]["area"] == pytest.approx(geodesic_area(coords), rel=1e-2)


def test_geometry_collection_partial_and_nested_folders(client):
    body = (
        "<Folder><name>Plots</name>"
        + placemark(kml_polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0)]))
        + "<Folder><name>Mixed</name>"
        + placemark(
            "<MultiGeometry><Point><coordinates>0,0</coordinates></Point>"
            + kml_line([(0, 0), (0.01, 0)])
            + "</MultiGeometry>"
        )
        + placemark("<MultiGeometry><Point><coordinates>0,0</coordinates></Point><Point><coordinates>1,1</coordinates></Point></MultiGeometry>")
        + "</Folder></Folder>"
    )
    r = upload(client, "nested.kml", kml(body))
    info = r.json()
    assert set(info["layers"]) == {"Plots", "Mixed"}
    page = items(client, info["id"], layer="Mixed")
    statuses = {i["geometry_type"]: i["status"] for i in page["items"]}
    assert statuses == {"GeometryCollection": "PARTIAL", "MultiPoint": "NOT_APPLICABLE"}
    partial = [i for i in page["items"] if i["status"] == "PARTIAL"]
    assert len(partial) == 1
    assert partial[0]["measurement"]["length"] == pytest.approx(geodesic_length([(0, 0), (0.01, 0)]), rel=1e-3)
    assert "points ignored" in partial[0]["note"]


def test_units_conversion(client, tmp_path):
    r = upload(client, "units.zip", shapefile_zip(tmp_path, [shapely.box(0, 0, 1000, 1000)], "EPSG:32633"))
    page = items(client, r.json()["id"], area_unit="ha", length_unit="km")
    m = page["items"][0]["measurement"]
    assert m["area"] == pytest.approx(100.0)
    assert m["perimeter"] == pytest.approx(4.0)
    assert m["area_unit"] == "ha" and m["length_unit"] == "km"
    assert page["summary"]["total_area"] == pytest.approx(100.0)


def test_shapefile_attributes_and_encoding(client, tmp_path):
    fields = {
        "name": np.array(["Café", "Ünïcode"], dtype=object),
        "value": np.array([1.5, np.nan]),
    }
    geoms = [shapely.box(0, 0, 1, 1), shapely.box(2, 2, 3, 3)]
    r = upload(client, "attrs.zip", shapefile_zip(tmp_path, geoms, "EPSG:32633", fields=fields))
    page = items(client, r.json()["id"], include="properties")
    assert page["items"][0]["properties"] == {"name": "Café", "value": 1.5}
    assert page["items"][1]["properties"]["value"] is None
