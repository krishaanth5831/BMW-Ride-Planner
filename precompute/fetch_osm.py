"""Fetch the rideable road network from the Overpass API, tiled and cached.

Why this exists: the earlier graph was built purely from crowd GPS traces, with
morton cells for node identity. That routes only where riders have been AND --
much worse -- its edges are straight lines between cell centroids, so a route
can visibly cut across fields and non-road land. Real OSM way geometry is the
only fix: if the graph IS the road network, a route cannot leave the roads.

Overpass etiquette: one tiled pass, cached to disk, a pause between requests,
and an honest User-Agent. Re-runs read the cache and hit nothing.
    https://overpass-api.de/api/interpreter
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://overpass-api.de/api/interpreter"
# Overpass returns 406 Not Acceptable to the default Python urllib User-Agent.
USER_AGENT = "BMW-Ride-Planner/0.1 (TUM.ai Zurich hackathon; contact via repo)"

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "fixtures", "osm")

# Demo region: Munich south through Starnberg, Tegernsee and toward Kesselberg --
# the roads rider A's own GPX files are named after.
# (*) Our chosen demo region and tile size -- they bound what the app can route
# over, but no source dictates them. Munich south through Starnberg, Tegernsee
# and toward Kesselberg, chosen because the crowd lake is densest there and
# rider A's own GPX files are named after those roads.
DEFAULT_BBOX = (47.60, 11.15, 48.25, 11.80)   # (*) south, west, north, east
TILE_DEG = 0.13                                # (*)

# Classes worth riding. residential/service/track are deliberately excluded:
# they multiply the download several times over and are not where anyone rides
# for pleasure. Link roads are kept because routing needs them for connectivity.
# (*) Which road classes count as "rideable" is our call. residential/service/
# track are excluded deliberately: they multiply the download several times over
# and are not where anyone rides for pleasure -- but that IS an opinion, and it
# means a start point on a quiet street snaps to the nearest larger road.
HIGHWAY_RE = (          # (*)
    "^(motorway|trunk|primary|secondary|tertiary|unclassified"
    "|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"
)

RETRIES = 6
PAUSE_S = 4.0


def _query(south: float, west: float, north: float, east: float) -> str:
    return (
        "[out:json][timeout:180];\n"
        f'way["highway"~"{HIGHWAY_RE}"]'
        f"({south:.4f},{west:.4f},{north:.4f},{east:.4f});\n"
        # `out geom;` includes node ids AND tags; `out geom tags;` drops the ids.
        "out geom;"
    )


def _fetch(q: str) -> dict:
    body = urllib.parse.urlencode({"data": q}).encode()
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(ENDPOINT, data=body,
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            # 429/504 are Overpass telling us to slow down; back off rather than
            # hammering a free public endpoint.
            time.sleep(PAUSE_S * (attempt + 1) * 3)
    raise RuntimeError(f"Overpass failed after {RETRIES} attempts: {last}")


def fetch_bbox(bbox=DEFAULT_BBOX, tile_deg: float = TILE_DEG,
               cache_dir: str = CACHE) -> str:
    """Fetch every tile in bbox, dedupe ways by id, write one combined file."""
    os.makedirs(cache_dir, exist_ok=True)
    south, west, north, east = bbox
    combined = os.path.join(
        cache_dir, f"ways_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}.json")
    if os.path.exists(combined):
        print(f"cache hit: {combined}")
        return combined

    tiles = []
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            tiles.append((lat, lon, min(lat + tile_deg, north), min(lon + tile_deg, east)))
            lon += tile_deg
        lat += tile_deg

    print(f"{len(tiles)} tiles to fetch")
    ways: dict[int, dict] = {}
    for i, t in enumerate(tiles, 1):
        tile_file = os.path.join(cache_dir, f"tile_{t[0]:.2f}_{t[1]:.2f}.json")
        if os.path.exists(tile_file):
            with open(tile_file) as fh:
                els = json.load(fh)
        else:
            t0 = time.time()
            els = _fetch(_query(*t))["elements"]
            with open(tile_file, "w") as fh:
                json.dump(els, fh)
            print(f"  [{i}/{len(tiles)}] {t[0]:.2f},{t[1]:.2f} "
                  f"-> {len(els):,} ways in {time.time() - t0:.0f}s", flush=True)
            time.sleep(PAUSE_S)
        for w in els:
            if w.get("type") == "way" and w.get("geometry"):
                ways[w["id"]] = w

    print(f"{len(ways):,} distinct ways")
    with open(combined, "w") as fh:
        json.dump(list(ways.values()), fh)
    print(f"wrote {combined} ({os.path.getsize(combined) / 1e6:.0f} MB)")
    return combined


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=",".join(str(x) for x in DEFAULT_BBOX))
    ap.add_argument("--tile", type=float, default=TILE_DEG)
    args = ap.parse_args()
    fetch_bbox(tuple(float(x) for x in args.bbox.split(",")), args.tile)


if __name__ == "__main__":
    main()
