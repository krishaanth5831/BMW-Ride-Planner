"""Build the routable graph from OSM ways, then snap BMW crowd stats onto it.

This replaces the crowd-only graph. The difference that matters: segments are
now real OSM way geometry, so a route physically cannot leave the road network.
The previous version connected morton cell centroids with straight lines, which
is why routes visibly cut across fields.

  1. Load cached Overpass ways (precompute/fetch_osm.py).
  2. Junction nodes = OSM nodes shared by 2+ ways, plus every way endpoint.
  3. Split each way at its junctions -> segments. Segment = routing edge = the
     unit everything is scored on.
  4. Aggregate the crowd lake per morton level-18 cell.
  5. Snap each cell onto the nearest segment (distance + heading agreement) and
     pool its stats there.

Usage:
    python -m precompute.build_osm_graph --out data
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import defaultdict

try:
    import duckdb
except ImportError:  # Dataset-free demo only needs the OSM helper functions.
    duckdb = None

from .fetch_osm import DEFAULT_BBOX
from .morton import LEVEL_NODE

GRID = 1 << LEVEL_NODE
DATASET = os.environ.get(
    "BMW_DATASET", os.path.expanduser("~/Desktop/BMW/exd_download/datasetHackathon"))

# ---------------------------------------------------------------------------
# (*) PROVENANCE MARKER
# A trailing  (*)  marks a number WE CHOSE OURSELVES -- not from the BMW
# dataset, an API, or the BMW brief. Unmarked values are traceable to a source,
# named in the comment. Full inventory: plan/PROVENANCE.md
# ---------------------------------------------------------------------------

# NOT invented: both come from BMW's own tripViewer (app.js:52 LEAN_STEP_NOISE,
# app.js:56 LEAN_STRAIGHT). We use their thresholds so our curviness matches
# the definition they shipped.
LEAN_STEP_NOISE = 2.0
LEAN_STRAIGHT = 5.0
# (*) GPS-dropout cutoff. Derived from the level-18 cell diagonal rather than
# stated anywhere, so: our choice, informed by geometry.
MAX_STEP_M = 150.0     # (*)
# NOT invented: measured from the data. ridingabsbraking takes values 0/1/2/3
# with 1 dominant; 2 and 3 are the engaged states.
ABS_ENGAGED = 2

# Snapping tolerance. BMW's map-matched positions sit a measured ~16 m from raw
# GPS with 62% inside 5 m, so they are already on road centrelines; 45 m is
# generous enough for that spread without leaping to a parallel road.
# (*) Our tolerance, but informed by a measurement: map-matched positions sit a
# measured mean 16 m from raw GPS with 62% inside 5 m.
SNAP_MAX_M = 45.0      # (*)
# (*) Index cell for candidate lookup (~400 m at this latitude). Purely an
# implementation detail -- affects speed, not results.
INDEX_LEVEL = 16       # (*)
INDEX_GRID = 1 << INDEX_LEVEL

# (*) Assumed speeds by road class (km/h). Used ONLY where the crowd has never
# ridden; observed speed always wins where we have it. OSM `maxspeed` is parsed
# but not yet preferred over these, which it should be.
CLASS_SPEED = {        # every value (*)
    "motorway": 120, "motorway_link": 80,
    "trunk": 95, "trunk_link": 70,
    "primary": 80, "primary_link": 60,
    "secondary": 70, "secondary_link": 55,
    "tertiary": 60, "tertiary_link": 50,
    "unclassified": 50,
}
# (*) Scenic desirability of each road class -- the single most opinionated
# table in the project, and entirely ours. The BRIEF supports the direction
# (inner city and standstills are red flags; curves and clear road view are
# green), but the numbers themselves are judgement, not measurement.
CLASS_SCENIC = {       # every value (*)
    "motorway": 0.02, "motorway_link": 0.05,
    "trunk": 0.25, "trunk_link": 0.25,
    "primary": 0.50, "primary_link": 0.45,
    "secondary": 0.75, "secondary_link": 0.65,
    "tertiary": 0.90, "tertiary_link": 0.80,
    "unclassified": 0.85,
}


def haversine_m(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def bearing_deg(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def point_seg_distance(p, a, b):
    """Metres from p to the a-b line piece, plus the fraction along it."""
    latf = 111_320.0
    lonf = 111_320.0 * math.cos(math.radians(p[0]))
    px, py = p[1] * lonf, p[0] * latf
    ax, ay = a[1] * lonf, a[0] * latf
    bx, by = b[1] * lonf, b[0] * latf
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


# ---------------------------------------------------------------- 1. segments
def ways_to_segments(ways: list[dict]) -> list[dict]:
    """Split OSM ways at junctions. Segment = routing edge = scoring unit.

    Overpass `out geom tags;` omits the node-id array, so junctions are derived
    from coincident coordinates instead. That is safe: every way referencing the
    same OSM node emits byte-identical lat/lon for it, so hashing the rounded
    coordinate recovers node identity exactly. It also catches the rare case of
    ways meeting at a shared position without a shared node id.
    """
    node_id: dict[tuple[float, float], int] = {}

    def nid(pt) -> int:
        key = (round(float(pt["lat"]), 7), round(float(pt["lon"]), 7))
        got = node_id.get(key)
        if got is None:
            got = len(node_id)
            node_id[key] = got
        return got

    # First pass: how many ways touch each node.
    use: dict[int, int] = defaultdict(int)
    ids_per_way: list[list[int]] = []
    for w in ways:
        geom = w.get("geometry") or []
        ids = [nid(g) for g in geom]
        ids_per_way.append(ids)
        for i in set(ids):
            use[i] += 1

    segments: list[dict] = []
    for w, ids in zip(ways, ids_per_way):
        geom = [(g["lat"], g["lon"]) for g in (w.get("geometry") or [])]
        if len(ids) < 2:
            continue
        tags = w.get("tags") or {}
        hw = tags.get("highway", "unclassified")

        # Cut at endpoints and at any interior node another way also uses.
        cuts = [0] + [i for i in range(1, len(ids) - 1) if use[ids[i]] > 1] + [len(ids) - 1]
        for i in range(len(cuts) - 1):
            s, e = cuts[i], cuts[i + 1]
            if e <= s:
                continue
            pts = geom[s:e + 1]
            length = sum(haversine_m(pts[k], pts[k + 1]) for k in range(len(pts) - 1))
            if length < 5:
                continue
            segments.append({
                "seg_id": len(segments),
                "node_a": ids[s],
                "node_b": ids[e],
                "way_id": int(w["id"]),
                "name": tags.get("name"),
                "highway": hw,
                "oneway": tags.get("oneway") in ("yes", "1", "true"),
                "maxspeed": tags.get("maxspeed"),
                "surface": tags.get("surface"),
                "tunnel": bool(tags.get("tunnel")),
                "length_m": length,
                "geometry": [[p[0], p[1]] for p in pts],
            })
    return segments


CHUNK_M = 100.0        # (*) scoring resolution, our choice


def subdivide(segments: list[dict]) -> list[dict]:
    """Cut junction-to-junction segments into uniform ~100 m chunks.

    Scoring at junction granularity is too coarse: a 2 km way averages one
    great corner away into a mediocre mean. 100 m is the resolution the crowd
    data actually supports, and because the geometry is still OSM the route
    stays physically on the road -- which a 100 m *grid* could not guarantee.

    Interior cut points get synthetic node ids above the OSM range so they can
    never collide with a real junction id.
    """
    out: list[dict] = []
    next_node = 10_000_000
    for seg in segments:
        pts = [tuple(p) for p in seg["geometry"]]
        if seg["length_m"] <= CHUNK_M * 1.5 or len(pts) < 2:
            out.append(dict(seg, seg_id=len(out)))
            continue

        # Walk the polyline, emitting a chunk every CHUNK_M metres.
        chunks: list[list[tuple[float, float]]] = []
        cur = [pts[0]]
        acc = 0.0
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            d = haversine_m(a, b)
            if d <= 0:
                continue
            t0 = 0.0
            while acc + d * (1 - t0) >= CHUNK_M:
                need = (CHUNK_M - acc) / d
                t0 += need
                cut = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
                cur.append(cut)
                chunks.append(cur)
                cur = [cut]
                acc = 0.0
                d_rem = d * (1 - t0)
                if d_rem <= 0:
                    break
            acc += d * (1 - t0)
            cur.append(b)
        if len(cur) > 1:
            chunks.append(cur)
        if not chunks:
            out.append(dict(seg, seg_id=len(out)))
            continue

        prev_node = seg["node_a"]
        for j, ch in enumerate(chunks):
            last = (j == len(chunks) - 1)
            if last:
                node_b = seg["node_b"]
            else:
                node_b = next_node
                next_node += 1
            length = sum(haversine_m(ch[k], ch[k + 1]) for k in range(len(ch) - 1))
            if length < 1:
                continue
            out.append(dict(seg,
                            seg_id=len(out),
                            node_a=prev_node,
                            node_b=node_b,
                            length_m=length,
                            geometry=[[x[0], x[1]] for x in ch]))
            prev_node = node_b
    return out


def geometric_curviness(pts: list[list[float]]) -> float:
    """Heading change per km from geometry, resampled to uniform spacing.

    OSM node spacing is wildly irregular -- a straight road may have nodes
    500 m apart while a hairpin has forty in 100 m, because a human traced it
    from imagery. Differentiating per node would measure mapping density, not
    curvature. So resample first.
    """
    STEP = 15.0
    if len(pts) < 3:
        return 0.0
    # Walk the polyline emitting a point every STEP metres.
    out = [tuple(pts[0])]
    carry = 0.0
    for i in range(len(pts) - 1):
        a, b = tuple(pts[i]), tuple(pts[i + 1])
        d = haversine_m(a, b)
        if d <= 0:
            continue
        t = (STEP - carry) / d
        while t <= 1.0:
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            t += STEP / d
        carry = (carry + d) % STEP
    if len(out) < 3:
        return 0.0
    total = 0.0
    for i in range(1, len(out) - 1):
        b1 = bearing_deg(out[i - 1], out[i])
        b2 = bearing_deg(out[i], out[i + 1])
        diff = abs(b2 - b1) % 360
        total += min(diff, 360 - diff)
    km = max(STEP * (len(out) - 1) / 1000.0, 1e-6)
    return total / km


# ------------------------------------------------------------------ 2. index
def build_index(segments: list[dict]) -> dict:
    idx: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for seg in segments:
        pts = seg["geometry"]
        for k in range(len(pts) - 1):
            for p in (pts[k], pts[k + 1]):
                cx = int(((p[1] + 180.0) / 360.0) * INDEX_GRID)
                cy = int(((p[0] + 90.0) / 360.0) * INDEX_GRID)
                idx[(cx, cy)].append((seg["seg_id"], k))
    return idx


def snap(point, heading, segments, idx):
    """Nearest segment piece, rejecting ones pointing the wrong way."""
    cx = int(((point[1] + 180.0) / 360.0) * INDEX_GRID)
    cy = int(((point[0] + 90.0) / 360.0) * INDEX_GRID)
    best = (None, SNAP_MAX_M + 1.0)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for seg_id, k in idx.get((cx + dx, cy + dy), ()):
                pts = segments[seg_id]["geometry"]
                d, _t = point_seg_distance(point, pts[k], pts[k + 1])
                if d >= best[1]:
                    continue
                if heading is not None:
                    b = bearing_deg(tuple(pts[k]), tuple(pts[k + 1]))
                    diff = abs(((heading - b + 180) % 360) - 180)
                    # A road running perpendicular to travel is the wrong road,
                    # however close it happens to be.
                    if min(diff, 180 - diff) > 55:
                        continue
                best = (seg_id, d)
    return best


# ------------------------------------------------------------- 3. crowd stats
def crowd_cells(bbox, shards: str) -> list[dict]:
    if duckdb is None:
        raise RuntimeError("duckdb is required to build graphs from BMW telemetry")
    south, west, north, east = bbox
    pattern = f"{DATASET}/anonymizedDataLake/{shards}/*.csv"
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='4GB'")
    con.execute("PRAGMA temp_directory='/tmp/duckdb_bmw'")
    sql = f"""
    WITH raw AS (
        SELECT filename AS trip, timestampinmillis AS ts,
               positionmapmatchedlatitude AS lat,
               positionmapmatchedlongitude AS lon,
               positionmapmatchedheading AS hdg,
               positionrawelevation AS elev, ridingvehiclespeed AS speed,
               sensorsbankingangle AS lean, ridingabsbraking AS absb,
               ridingenginespeed AS rpm,
               sensorsaccelerationlongitudinal AS acc_long
        FROM read_csv('{pattern}', filename = true, union_by_name = true,
                      ignore_errors = true)
        WHERE positionmapmatchedlatitude BETWEEN {south} AND {north}
          AND positionmapmatchedlongitude BETWEEN {west} AND {east}
    ),
    seq AS (
        SELECT *, lag(lat) OVER w AS p_lat, lag(lon) OVER w AS p_lon,
                  lag(lean) OVER w AS p_lean
        FROM raw WINDOW w AS (PARTITION BY trip ORDER BY ts)
    ),
    stepped AS (
        SELECT *,
            sqrt(power((lat - p_lat) * 111320.0, 2)
               + power((lon - p_lon) * 111320.0 * cos(radians(lat)), 2)) AS step_m,
            abs(lean - p_lean) AS d_lean
        FROM seq WHERE p_lat IS NOT NULL
    )
    SELECT
        CAST(floor(((lat + 90.0) / 360.0) * {GRID}) AS BIGINT) * {GRID}
      + CAST(floor(((lon + 180.0) / 360.0) * {GRID}) AS BIGINT)   AS cell,
        avg(lat) AS lat, avg(lon) AS lon,
        avg(hdg) AS hdg,
        count(DISTINCT trip) AS n_trips, count(*) AS n_points,
        sum(step_m) AS path_m,
        sum(CASE WHEN d_lean >= {LEAN_STEP_NOISE} THEN d_lean ELSE 0 END) AS lean_delta,
        sum(CASE WHEN abs(lean) > {LEAN_STRAIGHT} THEN step_m ELSE 0 END) AS leaned_m,
        quantile_cont(abs(lean), 0.95) AS lean_p95,
        avg(NULLIF(speed, 0)) AS speed_mean,
        quantile_cont(NULLIF(speed, 0), 0.85) AS speed_p85,
        avg(CASE WHEN speed BETWEEN 50 AND 120 THEN 1.0 ELSE 0.0 END) AS band_share,
        avg(CASE WHEN speed > 0 AND speed < 20 THEN 1.0 ELSE 0.0 END) AS crawl_share,
        sum(CASE WHEN absb >= {ABS_ENGAGED} THEN 1 ELSE 0 END) AS abs_events,
        sum(CASE WHEN acc_long < -3.0 THEN 1 ELSE 0 END) AS hard_decel,
        avg(NULLIF(rpm, 0)) AS rpm_mean,
        avg(NULLIF(elev, 0)) AS elev_mean
    FROM stepped
    WHERE step_m <= {MAX_STEP_M}
    GROUP BY cell
    """
    rows = con.execute(sql).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


# ------------------------------------------------------------------- 4. main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--shards", default="trips-samples-*/*")
    ap.add_argument("--bbox", default=",".join(str(x) for x in DEFAULT_BBOX))
    args = ap.parse_args()
    bbox = tuple(float(x) for x in args.bbox.split(","))
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()

    from .fetch_osm import fetch_bbox
    path = fetch_bbox(bbox)
    with open(path) as fh:
        ways = json.load(fh)
    print(f"[1/5] {len(ways):,} OSM ways", flush=True)

    segments = ways_to_segments(ways)
    print(f"[2/6] {len(segments):,} segments after splitting at junctions", flush=True)
    segments = subdivide(segments)
    print(f"[3/6] {len(segments):,} chunks after {CHUNK_M:.0f} m subdivision", flush=True)

    idx = build_index(segments)
    print(f"[4/6] spatial index: {len(idx):,} cells", flush=True)

    cells = crowd_cells(bbox, args.shards)
    print(f"[5/6] {len(cells):,} crowd cells in bbox ({time.time() - t0:.0f}s)", flush=True)

    acc = defaultdict(lambda: defaultdict(float))
    trips_on = defaultdict(set)
    snapped = 0
    for c in cells:
        if not c["lat"]:
            continue
        seg_id, dist = snap((c["lat"], c["lon"]), c.get("hdg"), segments, idx)
        if seg_id is None:
            continue
        snapped += 1
        a = acc[seg_id]
        for k in ("path_m", "lean_delta", "leaned_m", "abs_events", "hard_decel",
                  "n_points"):
            a[k] += float(c[k] or 0)
        for k in ("lean_p95", "speed_mean", "speed_p85", "band_share",
                  "crawl_share", "rpm_mean", "elev_mean"):
            if c[k] is not None:
                a[k] += float(c[k]) * float(c["n_points"] or 0)
                a[k + "_w"] += float(c["n_points"] or 0)
        a["n_trips"] = max(a["n_trips"], float(c["n_trips"] or 0))
    print(f"[6/6] snapped {snapped:,}/{len(cells):,} cells onto segments "
          f"({100 * snapped / max(len(cells), 1):.0f}%)", flush=True)

    from .fetch_dem import load_dem
    dem = load_dem(bbox)
    print(f"      terrain: {'Copernicus DEM' if dem.ok else 'unavailable'}", flush=True)

    for seg in segments:
        a = acc.get(seg["seg_id"])
        seg["curvature_geo"] = round(geometric_curviness(seg["geometry"]), 1)
        g = seg["geometry"]
        # Mean heading of the segment, for the sun-facing term in scenic(t).
        seg["bearing_mean"] = round(bearing_deg(tuple(g[0]), tuple(g[-1])), 1)
        seg["class_scenic"] = CLASS_SCENIC.get(seg["highway"], 0.5)
        if a:
            for k in ("path_m", "lean_delta", "leaned_m", "abs_events",
                      "hard_decel", "n_points", "n_trips"):
                seg[k] = a[k]
            for k in ("lean_p95", "speed_mean", "speed_p85", "band_share",
                      "crawl_share", "rpm_mean", "elev_mean"):
                w = a.get(k + "_w", 0.0)
                seg[k] = a[k] / w if w else 0.0
            ridden_km = max(seg["path_m"] / 1000.0, 1e-6)
            # Per ridden kilometre, so the ratio is trip-count invariant.
            seg["curviness"] = seg["lean_delta"] / ridden_km
            seg["leaned_share"] = (seg["leaned_m"] / seg["path_m"]) if seg["path_m"] else 0.0
            seg["abs_rate"] = seg["abs_events"] / ridden_km
            seg["hard_decel_rate"] = seg["hard_decel"] / ridden_km
        else:
            # No crowd coverage: fall back to geometry and road class. This is
            # the graceful degradation the confidence blend exists for -- an
            # unridden road still gets a score instead of vanishing.
            for k in ("path_m", "lean_delta", "leaned_m", "abs_events",
                      "hard_decel", "n_points", "n_trips", "lean_p95",
                      "band_share", "crawl_share", "rpm_mean",
                      "abs_rate", "hard_decel_rate", "leaned_share"):
                seg[k] = 0.0
            seg["curviness"] = seg["curvature_geo"]
            seg["speed_mean"] = 0.0
            seg["speed_p85"] = 0.0
            seg["elev_mean"] = 0.0
        seg["speed_assumed"] = CLASS_SPEED.get(seg["highway"], 50)

        # Terrain from the Copernicus DEM. Real elevation everywhere, including
        # roads no BMW rider has touched -- the trips' own GPS altitude only
        # covers crowd-ridden roads and is noisier.
        g = seg["geometry"]
        e0 = dem.sample(g[0][0], g[0][1])
        e1 = dem.sample(g[-1][0], g[-1][1])
        if e0 is not None and e1 is not None:
            seg["dem_elev_m"] = round((e0 + e1) / 2, 1)
            seg["dem_gradient_pct"] = round(
                100.0 * (e1 - e0) / max(seg["length_m"], 1.0), 2)
        else:
            seg["dem_elev_m"] = None
            seg["dem_gradient_pct"] = None

    # Local relief: elevation range within ~2 km of the segment. This is what
    # separates "high up" from "in the mountains" -- a plateau at 800 m is not
    # scenic the way a valley floor beneath peaks is.
    if dem.ok:
        for seg in segments:
            g = seg["geometry"]
            mid = g[len(g) // 2]
            vals = []
            for dla, dlo in ((0, 0), (0.018, 0), (-0.018, 0), (0, 0.027), (0, -0.027)):
                v = dem.sample(mid[0] + dla, mid[1] + dlo)
                if v is not None:
                    vals.append(v)
            seg["dem_relief_m"] = round(max(vals) - min(vals), 1) if len(vals) > 2 else None
    else:
        for seg in segments:
            seg["dem_relief_m"] = None

    adjacency: dict[int, list[int]] = defaultdict(list)
    for seg in segments:
        adjacency[seg["node_a"]].append(seg["seg_id"])
        if seg["node_b"] != seg["node_a"]:
            adjacency[seg["node_b"]].append(seg["seg_id"])

    covered = sum(1 for s in segments if s["n_trips"] > 0)
    meta = {
        "source": "osm+overpass",
        "bbox": list(bbox),
        "n_ways": len(ways),
        "n_segments": len(segments),
        "n_nodes": len(adjacency),
        "n_segments_with_crowd": covered,
        "crowd_coverage_pct": round(100 * covered / max(len(segments), 1), 1),
        "snap_max_m": SNAP_MAX_M,
        "chunk_m": CHUNK_M,
        "terrain": "copernicus-dem-30m" if dem.ok else None,
    }
    with open(os.path.join(args.out, "segments.json"), "w") as fh:
        json.dump(segments, fh)
    with open(os.path.join(args.out, "graph.json"), "w") as fh:
        json.dump({"adjacency": {str(k): v for k, v in adjacency.items()},
                   "meta": meta}, fh)
    print(f"\n{json.dumps(meta, indent=2)}")
    print(f"done in {time.time() - t0:.0f}s -> {args.out}/segments.json")


if __name__ == "__main__":
    main()
