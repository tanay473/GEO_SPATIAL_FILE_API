"""Rough latency benchmark against a running server: python scripts/bench.py [n_features] [base_url]"""

import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import shapely
from pyogrio import raw

n = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"

rng = np.random.default_rng(42)
lon = rng.uniform(68, 97, n)  # spread across several UTM zones
lat = rng.uniform(8, 35, n)
geoms = shapely.buffer(shapely.points(lon, lat), 0.002, quad_segs=6)  # ~25-vertex polygons

with tempfile.TemporaryDirectory() as tmp:
    shp = Path(tmp) / "bench.shp"
    raw.write(str(shp), shapely.to_wkb(geoms), [np.arange(n)], ["fid_src"],
              driver="ESRI Shapefile", geometry_type="Polygon", crs="EPSG:4326")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in Path(tmp).iterdir():
            zf.write(f, f.name)
data = buf.getvalue()
print(f"{n} polygons, zip {len(data) / 1e6:.2f} MB")

with httpx.Client(base_url=base, timeout=120) as c:
    t = time.perf_counter()
    r = c.post("/api/files/", files={"file": ("bench.zip", data)})
    print(f"POST {r.status_code} in {(time.perf_counter() - t) * 1000:.0f} ms "
          f"(server processing_ms={r.json().get('processing_ms')})")
    loc = r.headers["location"]
    while c.get(loc).json()["status"] in ("PENDING", "PROCESSING"):
        time.sleep(0.1)
    for label, url, params in [
        ("GET file", loc, {}),
        ("GET file+summary", loc, {"include": "summary"}),
        ("GET measurements (100)", f"{loc}measurements/", {}),
        ("GET measurements (1000)", f"{loc}measurements/", {"limit": 1000}),
        ("GET measurements (1000, geometry+props)", f"{loc}measurements/", {"limit": 1000, "include": "geometry,properties"}),
        ("GET single feature", f"{loc}measurements/", {"feature_id": n // 2, "include": "geometry,properties"}),
    ]:
        c.get(url, params=params)  # warm
        times = []
        for _ in range(20):
            t = time.perf_counter()
            c.get(url, params=params).raise_for_status()
            times.append((time.perf_counter() - t) * 1000)
        print(f"{label:<42} median {np.median(times):6.1f} ms  p95 {np.percentile(times, 95):6.1f} ms")
