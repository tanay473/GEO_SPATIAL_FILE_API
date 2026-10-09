"""API behaviour: validation errors, dedup, pagination, includes, async path."""

import io
import zipfile

import shapely

from helpers import kml, kml_polygon, placemark, shapefile_zip, upload

SQUARE = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01), (0, 0)]


def problem(r, status: int, code: str):
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"].endswith(code)


def test_unsupported_extension(client):
    problem(upload(client, "data.geojson", b"{}"), 415, "unsupported_file_type")


def test_kml_with_doctype_rejected(client):
    data = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml><Document></Document></kml>'
    problem(upload(client, "evil.kml", data), 400, "invalid_kml")


def test_not_really_kml(client):
    problem(upload(client, "fake.kml", b"hello world"), 415, "invalid_kml")


def test_kml_without_features(client):
    problem(upload(client, "empty.kml", kml("<name>nothing</name>")), 422, "no_features")


def test_empty_upload(client):
    problem(upload(client, "e.kml", b""), 400, "empty_file")


def test_corrupt_zip(client):
    problem(upload(client, "bad.zip", b"PK\x03\x04garbage"), 400, "invalid_zip")


def test_zip_without_dbf(client, tmp_path):
    data = shapefile_zip(tmp_path, [shapely.box(0, 0, 1, 1)], "EPSG:32633", drop=(".dbf",))
    problem(upload(client, "nodbf.zip", data), 400, "invalid_shapefile")


def test_zip_with_two_shapefiles(client, tmp_path):
    data = shapefile_zip(tmp_path, [shapely.box(0, 0, 1, 1)], "EPSG:32633", extra_entries={"other/b.shp": b"x"})
    problem(upload(client, "two.zip", data), 400, "invalid_shapefile")


def test_zip_slip_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.shp", b"x")
    problem(upload(client, "slip.zip", buf.getvalue()), 400, "invalid_zip")


def test_macosx_junk_ignored(client, tmp_path):
    data = shapefile_zip(
        tmp_path, [shapely.box(0, 0, 1, 1)], "EPSG:32633",
        extra_entries={"__MACOSX/layer/._data.shp": b"junk"},
    )
    assert upload(client, "mac.zip", data).status_code == 201


def test_upload_size_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 1024)
    problem(upload(client, "big.kml", kml(placemark(kml_polygon(SQUARE))) + b" " * 2048), 413, "file_too_large")


def test_feature_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_features", 1)
    body = placemark(kml_polygon(SQUARE), "a") + placemark(kml_polygon(SQUARE), "b")
    problem(upload(client, "many.kml", kml(body)), 413, "too_many_features")


def test_duplicate_upload_returns_existing(client):
    data = kml(placemark(kml_polygon(SQUARE), "dedup-me"))
    first = upload(client, "a.kml", data)
    second = upload(client, "b.kml", data)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_get_file_and_summary(client):
    r = upload(client, "info.kml", kml(placemark(kml_polygon(SQUARE), "info")))
    file_id = r.json()["id"]
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["filename"] == "info.kml"
    assert info["file_type"] == "KML"
    assert "summary" not in info
    with_summary = client.get(f"/api/files/{file_id}/", params={"include": "summary"}).json()
    assert with_summary["summary"]["measured"] == 1
    assert with_summary["summary"]["total_area"] > 0


def test_unknown_file_404(client):
    problem(client.get("/api/files/doesnotexist/"), 404, "not_found")
    problem(client.get("/api/files/doesnotexist/measurements/"), 404, "not_found")


def test_pagination_with_cursor(client, tmp_path):
    points = [shapely.Point(i, i) for i in range(250)]
    r = upload(client, "pts.zip", shapefile_zip(tmp_path, points, "EPSG:32633", geometry_type="Point"))
    file_id = r.json()["id"]
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 100} | ({"cursor": cursor} if cursor else {})
        page = client.get(f"/api/files/{file_id}/measurements/", params=params).json()
        seen += [i["feature_id"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3
    assert seen == list(range(250))
    problem(client.get(f"/api/files/{file_id}/measurements/", params={"cursor": "!!"}), 400, "invalid_cursor")


def test_single_feature_with_geometry_and_properties(client):
    r = upload(client, "one.kml", kml(placemark(kml_polygon(SQUARE), "only", "pm-1")))
    file_id = r.json()["id"]
    page = client.get(
        f"/api/files/{file_id}/measurements/", params={"feature_id": 0, "include": "geometry,properties"}
    ).json()
    item = page["items"][0]
    assert item["geometry"]["type"] == "Polygon"
    assert item["geometry"]["coordinates"][0][0] == [0.0, 0.0]
    assert item["properties"]["Name"] == "only"
    assert item["crs"] == "EPSG:4326"

    plain = client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
    assert "geometry" not in plain and "properties" not in plain

    problem(client.get(f"/api/files/{file_id}/measurements/", params={"feature_id": 99}), 404, "not_found")
    problem(client.get(f"/api/files/{file_id}/measurements/", params={"include": "bogus"}), 400, "invalid_include")


def test_filters(client):
    body = placemark(kml_polygon(SQUARE), "p") + placemark("<Point><coordinates>0,0</coordinates></Point>", "pt")
    file_id = upload(client, "filters.kml", kml(body)).json()["id"]
    polys = client.get(f"/api/files/{file_id}/measurements/", params={"geometry_type": "Polygon"}).json()
    assert [i["geometry_type"] for i in polys["items"]] == ["Polygon"]
    na = client.get(f"/api/files/{file_id}/measurements/", params={"status": "NOT_APPLICABLE"}).json()
    assert [i["geometry_type"] for i in na["items"]] == ["Point"]


def test_large_file_processed_in_background(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "sync_max_features", 0)
    r = upload(client, "async.kml", kml(placemark(kml_polygon(SQUARE), "async")))
    assert r.status_code == 202
    assert r.json()["status"] == "PENDING"
    # TestClient runs background tasks before returning, so processing is done now.
    info = client.get(r.headers["location"]).json()
    assert info["status"] == "COMPLETED"
    assert client.get(f"{r.headers['location']}measurements/").status_code == 200


def test_timestamps_are_utc(client):
    r = upload(client, "ts.kml", kml(placemark(kml_polygon(SQUARE), "ts")))
    for body in (r.json(), client.get(r.headers["location"]).json()):
        assert body["created_at"].endswith("Z") and body["processed_at"].endswith("Z")


def test_kml_network_link_is_not_fetched(client):
    import http.server
    import socketserver
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        link = f"<NetworkLink><Link><href>http://127.0.0.1:{srv.server_address[1]}/x.kml</href></Link></NetworkLink>"
        r = upload(client, "netlink.kml", kml(placemark(kml_polygon(SQUARE), "netlink") + link))
        srv.shutdown()
    assert r.status_code == 201
    assert r.json()["feature_count"] == 1
    assert hits == []
