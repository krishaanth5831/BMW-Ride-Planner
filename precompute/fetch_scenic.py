"""Scenic points worth riding TO, fetched once from Overpass and cached.

The whiteboard asks for scenic points "chained together instead of choosing
random curvy way". That needs actual places -- a lake shore, a viewpoint, an
alpine pass -- not just a curvature score. A curvy road through a housing
estate is curvy; it is not a destination.

What we take, and why each one earns its place:

    tourism=viewpoint       somebody signposted this as worth stopping at
    natural=water (big)     the lakes south of Munich are THE reason to ride
                            there; small ponds are noise, so there is an area
                            floor
    mountain_pass=yes       a named pass is a ride in itself
    natural=peak (prominent) only the ones with an elevation worth the climb

Overpass etiquette is inherited from fetch_osm: one tiled pass, cached to
disk, a pause between requests, an honest User-Agent, and re-runs that hit
nothing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from precompute.fetch_osm import ENDPOINT, PAUSE_S, RETRIES, USER_AGENT  # noqa: E402

CACHE = os.path.join(ROOT, "fixtures", "scenic")

# Same box as the cached road tiles: Munich south into the Alps.
BBOX = (47.60, 11.15, 48.25, 11.80)

MIN_LAKE_M2 = 150_000       # ~0.15 km2: Starnberger See yes, village pond no
MIN_PEAK_M = 1_000          # metres; below this it is a hill, not a view


def _query(south: float, west: float, north: float, east: float) -> str:
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:180];
(
  node["tourism"="viewpoint"]({box});
  node["mountain_pass"="yes"]({box});
  way["mountain_pass"="yes"]({box});
  node["natural"="peak"]["ele"]({box});
  way["natural"="water"]["name"]({box});
  relation["natural"="water"]["name"]({box});
);
out center bb tags;"""


# `bb` is what makes the lake filter possible: a lake's SIZE is the only thing
# separating Starnberger See from a village pond, and `out center` alone gives
# no extent at all.
def _fetch(q: str) -> dict:
    body = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                ENDPOINT, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 429 and 504 are Overpass asking us to slow down, not to give up.
            if e.code not in (429, 504) or attempt == RETRIES - 1:
                raise
        except Exception:
            if attempt == RETRIES - 1:
                raise
        wait = PAUSE_S * (attempt + 1)
        print(f"  overpass busy, waiting {wait:.0f}s", flush=True)
        time.sleep(wait)
    return {}


def _area_m2(el: dict) -> float:
    """Rough bounding-box area. Good enough to tell a lake from a pond."""
    b = el.get("bounds")
    if not b:
        return 0.0
    dlat = (b["maxlat"] - b["minlat"]) * 111_320.0
    dlon = (b["maxlon"] - b["minlon"]) * 111_320.0 * 0.67   # cos(48 deg)
    return abs(dlat * dlon)


def classify(el: dict) -> tuple[str, float] | None:
    """(kind, weight) or None if it does not earn a place."""
    t = el.get("tags", {})
    if t.get("tourism") == "viewpoint":
        return "viewpoint", 1.0
    if t.get("mountain_pass") == "yes":
        return "pass", 1.4
    if t.get("natural") == "peak":
        try:
            ele = float(str(t.get("ele", "")).split()[0])
        except (TypeError, ValueError):
            return None
        return ("peak", 1.0 + min(ele, 2500) / 2500) if ele >= MIN_PEAK_M else None
    if t.get("natural") == "water":
        if _area_m2(el) < MIN_LAKE_M2:
            return None
        # A big lake is worth more than a small one, but not without limit.
        return "lake", 1.0 + min(_area_m2(el) / 5e6, 0.8)
    return None


def main() -> int:
    os.makedirs(CACHE, exist_ok=True)
    out_path = os.path.join(CACHE, "poi_%.2f_%.2f_%.2f_%.2f.json" % BBOX)
    if os.path.exists(out_path):
        print(f"cache hit: {out_path}")
        return 0

    print(f"querying overpass for scenic points in {BBOX}...", flush=True)
    blob = _fetch(_query(*BBOX))

    pois, seen = [], set()
    for el in blob.get("elements", []):
        got = classify(el)
        if not got:
            continue
        kind, weight = got
        # Ways and relations come back with `bounds` but, for these, no
        # `center` -- so fall back to the middle of the bounding box. Without
        # this every lake is silently dropped for having no coordinate.
        c = el.get("center") or el
        lat, lon = c.get("lat"), c.get("lon")
        if (lat is None or lon is None) and el.get("bounds"):
            b = el["bounds"]
            lat = (b["minlat"] + b["maxlat"]) / 2
            lon = (b["minlon"] + b["maxlon"]) / 2
        if lat is None or lon is None:
            continue
        name = (el.get("tags", {}).get("name") or "").strip()
        # An unnamed viewpoint is still a viewpoint, but a lake we cannot name
        # is not something to put in front of a rider.
        if kind == "lake" and not name:
            continue
        key = (name or f"{kind}", round(lat, 3), round(lon, 3))
        if key in seen:
            continue
        seen.add(key)
        pois.append({
            "name": name or {"viewpoint": "Viewpoint", "pass": "Mountain pass",
                             "peak": "Summit"}.get(kind, kind.title()),
            "kind": kind, "lat": lat, "lon": lon, "weight": round(weight, 2),
            "ele": el.get("tags", {}).get("ele"),
        })

    pois.sort(key=lambda p: -p["weight"])
    with open(out_path, "w") as fh:
        json.dump({"bbox": BBOX, "pois": pois}, fh, indent=1)

    kinds: dict[str, int] = {}
    for p in pois:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"wrote {len(pois)} scenic points -> {out_path}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())))
    for p in pois[:8]:
        print(f"    {p['weight']:.2f}  {p['kind']:9s} {p['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
