# Geospatial File Measurement API

A FastAPI service that accepts a **zipped Shapefile** or a **KML** file, extracts every feature
(ID, geometry type, geometry, CRS, attributes) and returns **area** for polygons and **length**
for lines. Measurements are always done in a projected, metric CRS — never on lat/lon degrees.

- **3 endpoints**, exactly as specified: upload, file info, measurements
- Processed **once at upload**; all reads are indexed lookups (single-digit ms)
- Accuracy verified against geodesic ground truth (`pyproj.Geod`) in the test suite: **< 0.01 %** error

---

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/) (pip works too — see below).
GDAL, GEOS and PROJ ship inside the `pyogrio`/`shapely`/`pyproj` wheels; nothing to install system-wide.

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Interactive docs: http://127.0.0.1:8000/docs (Swagger) · http://127.0.0.1:8000/redoc

Without uv:

```bash
python -m venv .venv && .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --port 8000
```

Run the tests (33 tests, ~2 s):

```bash
uv run pytest
```

> **Windows tip:** call the API via `127.0.0.1`, not `localhost`. Windows tries IPv6 `::1` first, and
> uvicorn listens on IPv4 only, which adds ~2 s to every new connection.

### Configuration

All limits are environment variables with the `GEO_` prefix (or a `.env` file — see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `GEO_DATA_DIR` | `data` | Uploaded files + SQLite database |
| `GEO_DATABASE_URL` | SQLite in `GEO_DATA_DIR` | Any SQLAlchemy URL, e.g. Postgres |
| `GEO_MAX_UPLOAD_BYTES` | 50 MB | Upload size cap |
| `GEO_MAX_UNCOMPRESSED_BYTES` | 250 MB | Zip-bomb guard (sum of entries) |
| `GEO_MAX_COMPRESSION_RATIO` | 100 | Zip-bomb guard (per entry > 1 MB) |
| `GEO_MAX_ZIP_ENTRIES` | 100 | |
| `GEO_MAX_FEATURES` | 200 000 | |
| `GEO_MAX_VERTICES` | 10 000 000 | |
| `GEO_MAX_PROPERTY_CHARS` | 65 536 | Long KML descriptions are truncated |
| `GEO_SYNC_MAX_BYTES` / `GEO_SYNC_MAX_FEATURES` | 5 MB / 20 000 | Above either → processed in background (`202`) |
| `GEO_DEFAULT_PAGE_SIZE` / `GEO_MAX_PAGE_SIZE` | 100 / 1000 | |

---

## API

All errors use [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) `application/problem+json`:

```json
{
  "type": "https://errors.geo-api/invalid_zip",
  "title": "Invalid zip",
  "status": 400,
  "detail": "File is not a valid zip archive.",
  "instance": "/api/files/"
}
```

### `POST /api/files/` — upload & process

`multipart/form-data` with one field, `file`: a `.zip` containing exactly one Shapefile
(`.shp` + `.shx` + `.dbf`; `.prj`/`.cpg` optional) or a `.kml`.

```bash
curl -F "file=@samples/survey.kml" http://127.0.0.1:8000/api/files/
```

| Status | Meaning |
|---|---|
| `201 Created` | Processed during the request. Body = file info (below). `Location` header points to it. |
| `202 Accepted` | Large file queued (`status: "PENDING"`). Poll `GET /api/files/{id}/`. |
| `200 OK` | Identical bytes were uploaded before (SHA-256 match) — the existing record is returned instantly. |
| `400` | Corrupt zip, missing `.shx`/`.dbf`, more than one shapefile, unsafe paths, DOCTYPE/ENTITY in KML |
| `413` | Over size / feature / vertex limits |
| `415` | Not `.zip`/`.kml`, or content doesn't match (e.g. a KMZ renamed to `.kml`) |
| `422` | GDAL cannot read it, or it has no features |

### `GET /api/files/{id}/` — file information

Query: `include=summary` (optional) adds measurement totals.

```json
{
  "id": "7853debb80e3444c91eeb633169415b4",
  "filename": "survey.kml",
  "file_type": "KML",
  "size_bytes": 957,
  "sha256": "1e90521777cec7a808f8acd3fb43e2a083132a1894a56431b2a665f0ed557470",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "crs_is_geographic": true,
  "feature_count": 3,
  "geometry_types": { "Point": 1, "Polygon": 1, "LineString": 1 },
  "bbox": [77.59, 12.97, 77.6, 12.9735],
  "layers": ["Survey", "Plots", "Roads"],
  "warnings": [],
  "error": null,
  "created_at": "2026-10-09T11:30:11.474Z",
  "processed_at": "2026-10-09T11:30:11.497Z",
  "processing_ms": 21,
  "summary": {
    "total_area": 36009.382, "area_unit": "m2",
    "total_length": 1181.254, "length_unit": "m",
    "measured": 2, "partial": 0, "not_applicable": 1, "unsupported": 0, "failed": 0
  }
}
```

`status` lifecycle: `PENDING → PROCESSING → COMPLETED | FAILED` (with `error` set on failure).

### `GET /api/files/{id}/measurements/` — measurements (and feature details)

| Query param | Default | |
|---|---|---|
| `include` | — | Comma-separated `geometry`, `properties`. Omitted by default to keep responses small. |
| `feature_id` | — | Return just this feature (404 if absent) |
| `geometry_type` | — | e.g. `Polygon`, `LineString`, `GeometryCollection` |
| `status` | — | `MEASURED`, `PARTIAL`, `NOT_APPLICABLE`, `UNSUPPORTED`, `FAILED` |
| `layer` | — | KML folder / layer name |
| `area_unit` | `m2` | `m2`, `ha`, `km2` |
| `length_unit` | `m` | `m`, `km` (also applies to `perimeter`) |
| `limit` | 100 | max 1000 |
| `cursor` | — | `next_cursor` from the previous page |

Returns `409` while the file is still `PENDING`/`PROCESSING` (or if it `FAILED`).

```bash
curl "http://127.0.0.1:8000/api/files/7853debb80e3444c91eeb633169415b4/measurements/"
```

```json
{
  "file_id": "7853debb80e3444c91eeb633169415b4",
  "crs": "EPSG:4326",
  "summary": {
    "total_area": 36009.382, "area_unit": "m2",
    "total_length": 1181.254, "length_unit": "m",
    "measured": 2, "partial": 0, "not_applicable": 1, "unsupported": 0, "failed": 0
  },
  "items": [
    {
      "feature_id": 0, "source_id": "well-1", "layer": "Survey",
      "geometry_type": "Point", "crs": "EPSG:4326",
      "measurement": null, "measurement_crs": null,
      "status": "NOT_APPLICABLE", "note": "Points have no area or length"
    },
    {
      "feature_id": 1, "source_id": "plot-17", "layer": "Plots",
      "geometry_type": "Polygon", "crs": "EPSG:4326",
      "measurement": { "area": 36009.382, "perimeter": 765.881, "length": null, "area_unit": "m2", "length_unit": "m" },
      "measurement_crs": "+proj=tmerc +lat_0=0 +lon_0=77.5 +k=1 +ellps=WGS84",
      "status": "MEASURED", "note": null
    },
    {
      "feature_id": 2, "source_id": "road-3", "layer": "Roads",
      "geometry_type": "LineString", "crs": "EPSG:4326",
      "measurement": { "area": null, "perimeter": null, "length": 1181.254, "area_unit": "m2", "length_unit": "m" },
      "measurement_crs": "+proj=tmerc +lat_0=0 +lon_0=77.5 +k=1 +ellps=WGS84",
      "status": "MEASURED", "note": null
    }
  ],
  "next_cursor": null
}
```

One feature with geometry and attributes, in hectares/kilometres:

```bash
curl "http://127.0.0.1:8000/api/files/{id}/measurements/?feature_id=1&include=geometry,properties&area_unit=ha&length_unit=km"
```

```json
{
  "feature_id": 1, "source_id": "plot-17", "layer": "Plots",
  "geometry_type": "Polygon", "crs": "EPSG:4326",
  "measurement": { "area": 3.6009382, "perimeter": 0.765881, "length": null, "area_unit": "ha", "length_unit": "km" },
  "measurement_crs": "+proj=tmerc +lat_0=0 +lon_0=77.5 +k=1 +ellps=WGS84",
  "status": "MEASURED", "note": null,
  "geometry": { "type": "Polygon", "coordinates": [[[77.59, 12.97], [77.592, 12.97], [77.592, 12.9715], [77.59, 12.9715], [77.59, 12.97]]] },
  "properties": { "Name": "Plot 17", "description": null, "owner": "R. Kumar" }
}
```

`geometry` is GeoJSON **in the file's own CRS** (stated in `crs`), so projected Shapefiles are returned
in their native coordinates rather than silently reprojected.

### Measurement status per feature

| Geometry | Result | `status` |
|---|---|---|
| Polygon, MultiPolygon | `area` (m²) + `perimeter` (m) | `MEASURED` |
| LineString, MultiLineString, LinearRing | `length` (m) | `MEASURED` |
| Point, MultiPoint | — | `NOT_APPLICABLE` |
| GeometryCollection (KML MultiGeometry) | area of polygon parts, length of line parts | `PARTIAL` |
| Invalid polygon (self-intersecting etc.) | repaired with `make_valid`, then measured | `MEASURED` + `note` |
| Null / empty / unparseable (e.g. MultiPatch) | — | `UNSUPPORTED` + `note` |
| Unknown CRS that can't be inferred | — | `FAILED` + `note` |

A bad feature never fails the whole file.

---

## Architecture

```
app/
  main.py            app factory, middleware (gzip, early 413), startup warm-up
  config.py          settings / limits (pydantic-settings, GEO_* env vars)
  db.py, models.py   SQLAlchemy: `files` + `features` tables
  schemas.py         Pydantic response models (drive the OpenAPI docs)
  errors.py          ApiError -> problem+json
  api/files.py       the 3 routes
  services/
    upload.py        streamed save + SHA-256, zip / KML safety checks
    reader.py        GDAL read via pyogrio -> numpy arrays, attribute serialization
    crs.py           CRS parsing, projected-CRS selection, cached transformers
    measure.py       vectorized area / length / perimeter + per-feature status
    processor.py     pipeline orchestration, limits, bulk insert
tests/               33 tests: geodesic accuracy, edge cases, API behaviour
scripts/bench.py     latency benchmark against a running server
samples/survey.kml   example upload
```

### File-processing flow

```
POST /api/files/
  │ 1. extension check (.zip / .kml)                         → 415
  │ 2. stream to data/uploads/<id>/ in 1 MB chunks, hash,    → 413 (also pre-checked from Content-Length
  │    abort past the size cap                                     before the body is read)
  │ 3. safety checks
  │      zip: entry count, total uncompressed size, compression ratio, no "..", absolute paths,
  │           symlinks or encryption; ignore __MACOSX; exactly one .shp with .shx + .dbf  → 400
  │      kml: <kml> root present, reject DOCTYPE/ENTITY (billion-laughs / XXE)          → 400 / 415
  │ 4. SHA-256 already known? → return existing record (200)
  │ 5. cheap feature count from layer metadata (no geometry decoding)
  │ 6. small file → process now (201)   |   large file → background task (202)
  ▼
process_file
  read      pyogrio reads every layer straight out of the zip via /vsizip/ (no extraction),
            Z/M dropped, WKB → shapely arrays; KML folders become layers; LIBKML gives
            Placemark ids + ExtendedData
  limits    feature and vertex caps                                   → 413
  measure   per layer (see below)
  persist   one transaction: bulk Core INSERT of all feature rows + file-level aggregates
```

Data model: one `features` row holds the feature's index, layer, source id, type, 2D WKB geometry,
pre-serialized properties JSON **and** its measurement columns, keyed by `(file_id, feature_index)`.
Reads therefore never join, and file-level totals live on the `files` row.

### Measurement calculation flow

All per-feature maths is vectorized (shapely 2 / numpy over arrays of geometries); Python loops touch
only rare cases (collections, repaired or antimeridian features).

1. Classify: missing / empty / point → status set immediately, never projected.
2. Resolve CRS (next section) and project the measurable geometries to metres.
3. `is_valid` on polygonal geometries; invalid ones go through `make_valid` (a bowtie becomes
   two triangles) and get a note.
4. `shapely.area` / `shapely.length` on the whole array; perimeter = boundary length.
5. Collections: area of polygonal parts + length of lineal parts → `PARTIAL`.
6. Scale by the CRS unit (e.g. US survey feet → metres), round to mm.

### CRS handling

| Source CRS | Strategy |
|---|---|
| **Geographic** (EPSG:4326, NAD83, …) | Non-WGS84 datums are first transformed to WGS84. Each feature is then projected to a **local Transverse Mercator** (`k=1`) whose central meridian is the feature's centroid longitude snapped to a 0.5° grid. |
| Geographic, feature wider than 1° or crossing ±180° | **Lambert Azimuthal Equal-Area** centred on the feature (exact areas at any size). Antimeridian-crossing rings are first unwrapped (negative longitudes +360°). |
| **Projected** (UTM, State Plane, national grids…) | Measured natively — no reprojection — then scaled by the CRS's linear unit (feet → metres etc.). |
| **Missing** (Shapefile without `.prj`) | If all coordinates fit lon/lat bounds, assume EPSG:4326 and add a warning; otherwise the features are `FAILED: Unknown CRS` — we don't guess. |
| KML | Always WGS84 by specification. |

Each measurement reports the exact `measurement_crs` used, so any number can be reproduced.

---

## Design decisions

**FastAPI over Django/DRF.** The service is three endpoints over two tables; FastAPI gives typed
params, validation and OpenAPI docs with no ceremony, and its threadpool model suits CPU-bound
GDAL/GEOS work. Django would add an admin and ORM migrations we don't need.

**pyogrio + shapely 2 + pyproj, not geopandas/fiona.** pyogrio reads a layer into numpy arrays in one
GDAL call (several times faster than fiona's per-feature iteration) and can read zips in place via
`/vsizip/`. shapely 2's vectorized functions avoid a Python loop per feature. geopandas would have
added pandas as a dependency for no gain.

**Local Transverse Mercator instead of UTM** — *changed during implementation.* The original plan was
UTM zones. A test against geodesic truth showed a Bangalore plot (77.5°E, 2.5° from the zone 43
central meridian) measuring **0.10 % too large** — UTM's inherent scale distortion, which reaches
~0.2 % in area at zone edges. A TM centred within 0.25° of each feature keeps the error below
~0.01 % (tests assert 1e-4 relative error), while snapping to a 0.5° grid still lets nearby features
share one projection and one vectorized transform.
*Alternatives considered:* geodesic area via `pyproj.Geod` (very accurate, but the brief asks to
project first, and it's a per-feature Python call); a single equal-area CRS for everything
(exact area but distorted lengths); UTM (simplest, but less accurate as shown above).

**Measure projected data natively.** If the producer already chose a projected CRS, reprojecting
would cost time and could only add error.

**Compute once at upload, store everything.** GET requests are indexed reads of precomputed rows;
nothing is recalculated per request. Summaries are pre-aggregated on the file row.

**Sync for small files, background for large ones.** Most real uploads (hundreds to a few thousand
features) finish in tens of milliseconds, so making every client poll would add latency. The threshold
uses GDAL's cheap metadata feature count. Background processing uses FastAPI `BackgroundTasks`
(in-process); see Future scope.

**Content-addressed dedup.** Re-uploading the same bytes returns the existing result in ~25 ms
without re-processing.

**Measurements endpoint doubles as the feature endpoint.** The brief defines three endpoints, so
geometry/properties are opt-in via `include=` and a single feature via `feature_id=`, instead of
extra routes. Without `include`, the SQL query doesn't even load the geometry/properties columns.

**Speed-oriented details.**
- orjson serialization; stored GeoJSON/properties strings are embedded with `orjson.Fragment` (no parse/re-serialize).
- Keyset (cursor) pagination: constant cost per page regardless of depth.
- SQLAlchemy Core bulk insert (~1.5× faster than ORM bulk insert, still database-agnostic).
- PROJ pipelines built directly (`Transformer.from_pipeline`) — ~7× cheaper to create than
  `from_crs`, identical output — and cached.
- Startup warm-up pays GDAL/PROJ/DB initialisation once, not on the first request.
- gzip at level 5 (level 9 costs ~3× the CPU for a few % smaller bodies).
- SQLite in WAL mode so reads aren't blocked by a background write.

**Security posture.** Size checks before and during streaming; zip-bomb, zip-slip, symlink and
encrypted-entry checks; KML DOCTYPE/ENTITY rejected; NetworkLinks are never fetched; uploads are stored
under server-generated names, never the client's filename.

### Measured performance

On a Windows laptop, SQLite, with `scripts/bench.py` (randomly placed ~25-vertex polygons over India):

| Operation | Latency |
|---|---|
| Upload + process, 1 polygon KML | ~20–30 ms |
| Upload + process, 2 000 polygons (0.44 MB zip) | ~120 ms processing |
| Upload + process, 20 000 polygons / 500 k vertices (4.4 MB zip) | ~1.1 s processing (read 0.2 s · project+measure 0.3 s · insert 0.4 s) |
| Duplicate upload (dedup hit) | ~25 ms |
| `GET /api/files/{id}/` | ~3 ms |
| `GET …/measurements/` (100 items) | ~6 ms |
| `GET …/measurements/` (1 000 items) | ~28 ms |
| `GET …/measurements/?feature_id=…&include=geometry,properties` | ~4 ms |

```bash
uv run uvicorn app.main:app --port 8000      # terminal 1
uv run python scripts/bench.py 20000         # terminal 2
```

---

## Learnings

- **"Project to UTM" isn't automatically accurate.** UTM's 0.9996 central scale factor and 6° zones
  give up to ~0.2 % area error; only testing against geodesic ground truth exposed it.
- **KML is more varied than it looks.** Nested folders become separate GDAL layers, MultiGeometry can
  mix types, and the LIBKML driver adds a dozen styling fields to every feature that have to be
  filtered out of the attributes.
- **Shapefiles in the wild are messy.** Missing `.prj`, macOS `__MACOSX` junk, several shapefiles in one
  zip and legacy encodings are all normal; the code has to decide explicitly what to do with each.
- **Profile before optimising.** The real costs were PROJ coordinate maths, the DB insert and
  cold-start initialisation, not where intuition pointed. One "slow first request" turned out to be
  the *client* (httpx) reading the Windows MIME registry.

## Future scope

- **Durable job queue** (ARQ/Celery + Redis) so queued work survives restarts and scales across workers;
  today `BackgroundTasks` runs in the API process.
- **PostGIS** for spatial queries (bbox filters, `ST_Area(geography)` cross-checks) and `COPY`-based inserts.
- **More formats:** KMZ, GeoJSON, GeoPackage, multiple shapefiles per zip as layers.
- **Geodesic mode** (`?method=geodesic` using `pyproj.Geod`) for continent-scale features and polygons containing a pole.
- **Object storage** (S3/GCS) for uploads, plus a retention / `DELETE` policy.
- Auth, rate limiting, request IDs and metrics (Prometheus / OpenTelemetry).
- Dockerfile and CI (lint, type-check, tests).
- Hard per-job timeouts by running processing in a subprocess pool that can be killed.
