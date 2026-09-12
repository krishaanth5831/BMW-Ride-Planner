"""Copernicus DEM (GLO-30) via Sentinel Hub, fetched once and cached.

Auth is OAuth client-credentials against the Copernicus Data Space, then a
Bearer token on the Process API. Credentials come from .env and are never
committed.

We pull one float32 GeoTIFF for the whole demo bbox and sample it locally.
That is a single request instead of tens of thousands of point lookups, and it
makes the elevation term work offline afterwards.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "fixtures", "dem")
TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
             "/protocol/openid-connect/token")
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Sentinel Hub caps a single request at 2500x2500 px.
MAX_PX = 2500

EVALSCRIPT = """//VERSION=3
function setup() {
  return { input: ["DEM"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(sample) { return [sample.DEM]; }
"""


def _env() -> dict:
    path = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    out.setdefault("COPERNICUS_CLIENT_ID", os.environ.get("COPERNICUS_CLIENT_ID", ""))
    out.setdefault("COPERNICUS_CLIENT_SECRET",
                   os.environ.get("COPERNICUS_CLIENT_SECRET", ""))
    return out


def get_token() -> str:
    env = _env()
    if not env.get("COPERNICUS_CLIENT_ID"):
        raise RuntimeError("no COPERNICUS_CLIENT_ID -- put it in .env")
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": env["COPERNICUS_CLIENT_ID"],
        "client_secret": env["COPERNICUS_CLIENT_SECRET"],
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data),
                                timeout=60) as r:
        return json.load(r)["access_token"]


def fetch_dem(bbox, cache_dir: str = CACHE) -> str | None:
    """Fetch the DEM tile for bbox (south, west, north, east). Returns a path.

    Returns None (rather than raising) if the service is unreachable or
    unauthorised -- elevation is an enhancement, not a hard dependency, and the
    rest of the scoring must keep working without it.
    """
    os.makedirs(cache_dir, exist_ok=True)
    south, west, north, east = bbox
    path = os.path.join(cache_dir, f"dem_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}.tif")
    meta_path = path + ".json"
    if os.path.exists(path):
        print(f"DEM cache hit: {path}")
        return path

    # ~30 m ground sampling, clamped to the API's per-request pixel cap.
    width = min(MAX_PX, max(64, int((east - west) * 111_320 * 0.66 / 30)))
    height = min(MAX_PX, max(64, int((north - south) * 111_320 / 30)))

    body = {
        "input": {
            "bounds": {
                "bbox": [west, south, east, north],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{"type": "dem", "dataFilter": {"demInstance": "COPERNICUS_30"}}],
        },
        "output": {
            "width": width, "height": height,
            "responses": [{"identifier": "default",
                           "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT,
    }

    try:
        token = get_token()
        req = urllib.request.Request(
            PROCESS_URL, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "image/tiff"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
        with open(path, "wb") as fh:
            fh.write(raw)
        with open(meta_path, "w") as fh:
            json.dump({"bbox": list(bbox), "width": width, "height": height}, fh)
        print(f"DEM {width}x{height} px, {len(raw) / 1e6:.1f} MB in {time.time() - t0:.0f}s")
        return path
    except Exception as e:
        detail = getattr(e, "read", lambda: b"")()
        print(f"DEM fetch failed ({e}) {detail[:200]!r} -- continuing without terrain")
        return None


class DEM:
    """Bilinear-ish sampler over the cached tile. No-op if there is no tile."""

    def __init__(self, bbox, cache_dir: str = CACHE):
        self.ok = False
        south, west, north, east = bbox
        path = os.path.join(cache_dir, f"dem_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}.tif")
        if not os.path.exists(path):
            return
        try:
            import tifffile
            self.arr = np.asarray(tifffile.imread(path), dtype="float32")
            if self.arr.ndim == 3:
                self.arr = self.arr[..., 0]
            self.south, self.west, self.north, self.east = bbox
            self.h, self.w = self.arr.shape
            self.ok = True
        except Exception as e:
            print(f"DEM load failed: {e}")

    def sample(self, lat: float, lon: float) -> float | None:
        if not self.ok:
            return None
        # Row 0 is the NORTH edge in an image raster.
        fy = (self.north - lat) / (self.north - self.south) * (self.h - 1)
        fx = (lon - self.west) / (self.east - self.west) * (self.w - 1)
        if not (0 <= fy <= self.h - 1 and 0 <= fx <= self.w - 1):
            return None
        v = float(self.arr[int(round(fy)), int(round(fx))])
        # Copernicus uses large negative values for no-data.
        return None if v < -1000 or v > 9000 else v


# ---------------------------------------------------------------------------
# Keyless fallback: Copernicus DEM GLO-30 is also published as public COGs on
# AWS. Used when Sentinel Hub returns 403 (the CDSE account has an OAuth token
# but no Process API entitlement), which is the common case on a free account.
AWS_TMPL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
            "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
            "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")


def _tile_name(lat: int, lon: int) -> str:
    return AWS_TMPL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                           ew="E" if lon >= 0 else "W", lon=abs(lon))


def fetch_dem_aws(bbox, cache_dir: str = CACHE, step: int = 3) -> str | None:
    """Download the 1-degree tiles covering bbox and cache one downsampled grid.

    `step` decimates the 30 m grid (step=3 -> ~90 m), which is far finer than
    the 100 m scoring chunks and keeps the cached array small.
    """
    import tifffile

    os.makedirs(cache_dir, exist_ok=True)
    south, west, north, east = bbox
    out = os.path.join(cache_dir, f"aws_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}.npz")
    if os.path.exists(out):
        print(f"DEM cache hit: {out}")
        return out

    lat0, lat1 = int(np.floor(south)), int(np.floor(north))
    lon0, lon1 = int(np.floor(west)), int(np.floor(east))
    rows = []
    for la in range(lat1, lat0 - 1, -1):          # north to south
        cols = []
        for lo in range(lon0, lon1 + 1):
            url = _tile_name(la, lo)
            local = os.path.join(cache_dir, os.path.basename(url))
            if not os.path.exists(local):
                print(f"  downloading {os.path.basename(url)} ...", flush=True)
                try:
                    with urllib.request.urlopen(url, timeout=600) as r, open(local, "wb") as fh:
                        fh.write(r.read())
                except Exception as e:
                    print(f"  tile {la},{lo} unavailable ({e}); filling with no-data")
                    cols.append(None)
                    continue
            a = np.asarray(tifffile.imread(local), dtype="float32")[::step, ::step]
            cols.append(a)
        widths = [c.shape[1] for c in cols if c is not None]
        heights = [c.shape[0] for c in cols if c is not None]
        if not widths:
            continue
        h, w = max(heights), max(widths)
        cols = [c if c is not None else np.full((h, w), -32768, "float32") for c in cols]
        rows.append(np.hstack(cols))
    if not rows:
        return None
    grid = np.vstack(rows)
    # Tiles are whole degrees; record the exact extent they cover.
    np.savez_compressed(out, grid=grid,
                        extent=np.array([lat0, lon0, lat1 + 1, lon1 + 1], "float64"))
    print(f"DEM grid {grid.shape} cached -> {out}")
    return out


class DEMGrid:
    """Sampler over the AWS-derived grid."""

    def __init__(self, bbox, cache_dir: str = CACHE):
        self.ok = False
        south, west, north, east = bbox
        path = os.path.join(cache_dir, f"aws_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}.npz")
        if not os.path.exists(path):
            return
        z = np.load(path)
        self.grid = z["grid"]
        self.s, self.w_, self.n, self.e = z["extent"]
        self.h, self.wd = self.grid.shape
        self.ok = True

    def sample(self, lat: float, lon: float):
        if not self.ok:
            return None
        fy = (self.n - lat) / (self.n - self.s) * (self.h - 1)
        fx = (lon - self.w_) / (self.e - self.w_) * (self.wd - 1)
        if not (0 <= fy <= self.h - 1 and 0 <= fx <= self.wd - 1):
            return None
        v = float(self.grid[int(round(fy)), int(round(fx))])
        return None if v < -1000 or v > 9000 else v


def load_dem(bbox):
    """Best available terrain source, or a no-op sampler."""
    g = DEMGrid(bbox)
    if g.ok:
        return g
    d = DEM(bbox)
    if d.ok:
        return d

    class _Null:
        ok = False

        def sample(self, lat, lon):
            return None

    return _Null()


if __name__ == "__main__":
    from .fetch_osm import DEFAULT_BBOX
    p = fetch_dem(DEFAULT_BBOX) or fetch_dem_aws(DEFAULT_BBOX)
    if p:
        d = load_dem(DEFAULT_BBOX)
        print("sample Munich 48.14,11.58 ->", d.sample(48.14, 11.58), "m")
        print("sample Kesselberg 47.60,11.31 ->", d.sample(47.60, 11.31), "m")
        print("sample Tegernsee 47.71,11.76 ->", d.sample(47.71, 11.76), "m")
