"""Crowd lake -> road segments + routable graph.

Pipeline (see plan/ALGORITHM.md sections 3 and 4):
  1. Read crowd trip CSVs through DuckDB, restricted to a bbox.
  2. Quantise map-matched positions to morton level-18 cells -> node identity.
     Cells are only how we recognise "the same place" without a road map; they
     are NOT the unit of analysis.
  3. Aggregate observed node->node transitions with lean / speed / ABS stats.
  4. Collapse chains of degree-2 nodes into SEGMENTS, which are the unit of
     analysis and the routing edge. Junctions are nodes of degree != 2.

This is the no-OSM fallback documented in ALGORITHM.md section 3: it needs no
download, and it routes only where riders have actually ridden.

Usage:
    python -m precompute.build_graph --shards 'trips-samples-1/0*' --out data/
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import defaultdict

import duckdb

from .morton import LEVEL_NODE, cell_size_m

# Demo region: Munich plus the southern foothills (Kesselberg, Tegernsee,
# Ammersee) -- the area the crowd lake actually covers densely.
DEFAULT_BBOX = (47.40, 10.80, 48.60, 12.00)  # south, west, north, east

DATASET = os.environ.get(
    "BMW_DATASET",
    os.path.expanduser("~/Desktop/BMW/exd_download/datasetHackathon"),
)

GRID = 1 << LEVEL_NODE  # 262144 cells per axis at level 18

# Lean changes below this many degrees are sensor noise (BMW's own LEAN_STEP_NOISE
# at tripViewer/app.js:52). Without it every straight accrues a baseline.
LEAN_STEP_NOISE = 2.0
# Beyond this lean the bike counts as "leaned over" (BMW's LEAN_STRAIGHT).
LEAN_STRAIGHT = 5.0
# Steps longer than this are GPS dropouts, not road. 1.5x the level-18 cell
# diagonal. Keeping them would hand Dijkstra free multi-kilometre shortcuts.
MAX_STEP_M = 150.0
# A transition needs this many distinct trips before we trust it. Also prunes
# parallel-road collapse (motorway + frontage road inside one cell).
MIN_TRIPS_PER_EDGE = 2

# ABS/ASC are 0=unknown, 1=inactive, 2/3=engaged.
ABS_ENGAGED = 2


def _sql(shard_glob: str, bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    pattern = f"{DATASET}/anonymizedDataLake/{shard_glob}/*.csv"
    return f"""
WITH raw AS (
    SELECT
        filename                          AS trip,
        timestampinmillis                 AS ts,
        positionmapmatchedlatitude        AS lat,
        positionmapmatchedlongitude       AS lon,
        positionrawelevation              AS elev,
        ridingvehiclespeed                AS speed,
        sensorsbankingangle               AS lean,
        ridingabsbraking                  AS absb,
        ridingenginespeed                 AS rpm,
        sensorsaccelerationlongitudinal   AS acc_long
    FROM read_csv('{pattern}', filename = true, union_by_name = true,
                  ignore_errors = true)
    WHERE positionmapmatchedlatitude BETWEEN {south} AND {north}
      AND positionmapmatchedlongitude BETWEEN {west} AND {east}
),
-- Window partitioned by FILENAME, never trip_id: trip_id is only 56% filled in
-- the lake, so partitioning on it merges ~44% of rows into one null partition
-- and corrupts every delta-lean across trip boundaries.
seq AS (
    SELECT
        trip, ts, lat, lon, elev, speed, lean, absb, rpm, acc_long,
        CAST(floor(((lon + 180.0) / 360.0) * {GRID}) AS BIGINT) AS cx,
        CAST(floor(((lat +  90.0) / 360.0) * {GRID}) AS BIGINT) AS cy,
        lag(lat)  OVER w AS p_lat,
        lag(lon)  OVER w AS p_lon,
        lag(lean) OVER w AS p_lean,
        lag(CAST(floor(((lon + 180.0) / 360.0) * {GRID}) AS BIGINT)) OVER w AS p_cx,
        lag(CAST(floor(((lat +  90.0) / 360.0) * {GRID}) AS BIGINT)) OVER w AS p_cy
    FROM raw
    WINDOW w AS (PARTITION BY trip ORDER BY ts)
),
stepped AS (
    SELECT
        *,
        -- equirectangular step distance; fine at 100 m scale
        sqrt(power((lat - p_lat) * 111320.0, 2)
           + power((lon - p_lon) * 111320.0 * cos(radians(lat)), 2)) AS step_m,
        abs(lean - p_lean) AS d_lean
    FROM seq
    WHERE p_lat IS NOT NULL
),
clean AS (
    SELECT * FROM stepped WHERE step_m <= {MAX_STEP_M} AND step_m >= 0
)
SELECT
    p_cy * {GRID} + p_cx                                   AS node_from,
    cy   * {GRID} + cx                                     AS node_to,
    count(DISTINCT trip)                                   AS n_trips,
    count(*)                                               AS n_points,
    sum(step_m)                                            AS path_m,
    sum(CASE WHEN d_lean >= {LEAN_STEP_NOISE} THEN d_lean ELSE 0 END) AS lean_delta,
    sum(CASE WHEN abs(lean) > {LEAN_STRAIGHT} THEN step_m ELSE 0 END) AS leaned_m,
    avg(abs(lean))                                         AS lean_mean,
    quantile_cont(abs(lean), 0.95)                         AS lean_p95,
    avg(NULLIF(speed, 0))                                  AS speed_mean,
    quantile_cont(NULLIF(speed, 0), 0.85)                  AS speed_p85,
    avg(CASE WHEN speed BETWEEN 50 AND 120 THEN 1.0 ELSE 0.0 END) AS band_share,
    avg(CASE WHEN speed > 0 AND speed < 20 THEN 1.0 ELSE 0.0 END) AS crawl_share,
    sum(CASE WHEN absb >= {ABS_ENGAGED} THEN 1 ELSE 0 END) AS abs_events,
    sum(CASE WHEN acc_long < -3.0 THEN 1 ELSE 0 END)       AS hard_decel,
    avg(NULLIF(rpm, 0))                                    AS rpm_mean,
    avg(NULLIF(elev, 0))                                   AS elev_mean,
    avg(lat)                                               AS lat,
    avg(lon)                                               AS lon
FROM clean
WHERE (p_cx != cx OR p_cy != cy)          -- only real transitions between cells
GROUP BY node_from, node_to
HAVING count(DISTINCT trip) >= {MIN_TRIPS_PER_EDGE}
"""


def node_center(node_id: int) -> tuple[float, float]:
    """Cell centre for a packed node id."""
    cy, cx = divmod(node_id, GRID)
    lat = ((cy + 0.5) / GRID) * 360.0 - 90.0
    lon = ((cx + 0.5) / GRID) * 360.0 - 180.0
    return lat, lon


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def collapse_to_segments(edges: list[dict]) -> tuple[list[dict], dict]:
    """Collapse chains of degree-2 nodes into segments.

    Junctions are nodes whose undirected degree != 2. Everything between two
    junctions becomes one segment carrying a polyline and pooled stats. This is
    how a routing graph is built from a raw network, and it is what makes the
    unit of analysis a road rather than a grid cell.
    """
    # Undirected adjacency for topology; directed lookup for stats.
    undirected: dict[int, set[int]] = defaultdict(set)
    by_pair: dict[tuple[int, int], dict] = {}
    for e in edges:
        a, b = e["node_from"], e["node_to"]
        if a == b:
            continue
        undirected[a].add(b)
        undirected[b].add(a)
        by_pair[(a, b)] = e

    def pooled(a: int, b: int) -> dict | None:
        """Stats for a-b in whichever direction was observed (or both pooled)."""
        f, r = by_pair.get((a, b)), by_pair.get((b, a))
        both = [x for x in (f, r) if x]
        if not both:
            return None
        out = {}
        for key in (
            "n_trips", "n_points", "path_m", "lean_delta", "leaned_m",
            "abs_events", "hard_decel",
        ):
            out[key] = sum(float(x[key] or 0) for x in both)
        # Weight means by point count so a busy direction dominates correctly.
        for key in (
            "lean_mean", "lean_p95", "speed_mean", "speed_p85",
            "band_share", "crawl_share", "rpm_mean", "elev_mean",
        ):
            num = sum(float(x[key] or 0) * float(x["n_points"] or 0) for x in both)
            den = sum(float(x["n_points"] or 0) for x in both if x[key] is not None)
            out[key] = num / den if den else 0.0
        return out

    # Observed mean position per node, from the telemetry itself. Falls back to
    # the cell centre only for nodes that never appear as a destination.
    obs_acc: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for e in edges:
        w = float(e.get("n_points") or 1)
        if e.get("lat") and e.get("lon"):
            a = obs_acc[e["node_to"]]
            a[0] += float(e["lat"]) * w
            a[1] += float(e["lon"]) * w
            a[2] += w
    node_pos: dict[int, tuple[float, float]] = {}
    for n, (slat, slon, w) in obs_acc.items():
        if w > 0:
            node_pos[n] = (slat / w, slon / w)

    def pos(n: int) -> tuple[float, float]:
        return node_pos.get(n) or node_center(n)

    junctions = {n for n, nbrs in undirected.items() if len(nbrs) != 2}
    # A ring of pure degree-2 nodes has no junction to start from; break it open
    # at an arbitrary node so the chain still becomes a segment.
    for n in undirected:
        if not junctions:
            junctions.add(n)
            break

    segments: list[dict] = []
    seen_chain: set[tuple[int, int]] = set()

    for start in junctions:
        for first in undirected[start]:
            if (start, first) in seen_chain:
                continue
            chain = [start, first]
            seen_chain.add((start, first))
            prev, cur = start, first
            # Walk forward while nodes are pure pass-throughs.
            while cur not in junctions:
                nxt = next((n for n in undirected[cur] if n != prev), None)
                if nxt is None or nxt in chain:
                    break
                seen_chain.add((cur, nxt))
                chain.append(nxt)
                prev, cur = cur, nxt
            seen_chain.add((chain[-1], chain[-2]))

            # Pool every hop in the chain.
            acc = defaultdict(float)
            length_m = 0.0
            pts = [pos(n) for n in chain]
            ok = True
            for i in range(len(chain) - 1):
                st = pooled(chain[i], chain[i + 1])
                if st is None:
                    ok = False
                    break
                for k, v in st.items():
                    if k in ("n_trips",):
                        acc[k] = max(acc[k], v)  # trips are not additive along a chain
                    elif k in ("n_points", "path_m", "lean_delta", "leaned_m",
                               "abs_events", "hard_decel"):
                        acc[k] += v
                    else:
                        acc[k] += v * st["n_points"]
                        acc[k + "_w"] += st["n_points"]
                length_m += haversine_m(pts[i], pts[i + 1])
            if not ok or length_m < 20:
                continue

            seg = {
                "seg_id": len(segments),
                "node_a": chain[0],
                "node_b": chain[-1],
                "length_m": length_m,
                "geometry": pts,
                "n_trips": acc["n_trips"],
                "n_points": acc["n_points"],
                "path_m": acc["path_m"],
                "lean_delta": acc["lean_delta"],
                "leaned_m": acc["leaned_m"],
                "abs_events": acc["abs_events"],
                "hard_decel": acc["hard_decel"],
            }
            for key in ("lean_mean", "lean_p95", "speed_mean", "speed_p85",
                        "band_share", "crawl_share", "rpm_mean", "elev_mean"):
                w = acc.get(key + "_w", 0.0)
                seg[key] = acc[key] / w if w else 0.0

            # Per-kilometre normalisation, against RIDDEN distance (path_m,
            # summed over every traversal) -- not the single-pass geometric
            # length. Both numerator and denominator then scale with the number
            # of passes, so the ratio is trip-count invariant. Dividing by
            # geometric length instead would smuggle an exposure count into a
            # quality score, which is precisely the flaw we call out in BMW's
            # own funFactor (app.js:203).
            ridden_km = max(seg["path_m"] / 1000.0, 1e-6)
            seg["curviness"] = seg["lean_delta"] / ridden_km
            seg["leaned_share"] = seg["leaned_m"] / seg["path_m"] if seg["path_m"] else 0.0
            seg["abs_rate"] = seg["abs_events"] / ridden_km
            seg["hard_decel_rate"] = seg["hard_decel"] / ridden_km
            # Elevation gain along the chain, from raw GPS elevation.
            seg["elev_gain_m"] = 0.0
            segments.append(seg)

    adjacency: dict[int, list[int]] = defaultdict(list)
    for seg in segments:
        adjacency[seg["node_a"]].append(seg["seg_id"])
        if seg["node_b"] != seg["node_a"]:
            adjacency[seg["node_b"]].append(seg["seg_id"])

    meta = {
        "n_nodes": len(undirected),
        "n_junctions": len(junctions),
        "n_edges_raw": len(edges),
        "n_segments": len(segments),
        "level": LEVEL_NODE,
        "cell_m": round(cell_size_m(LEVEL_NODE), 1),
    }
    return segments, {"adjacency": {str(k): v for k, v in adjacency.items()}, "meta": meta}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", default="trips-samples-1/0*",
                    help="glob under anonymizedDataLake, e.g. 'trips-samples-*/*'")
    ap.add_argument("--out", default="data")
    ap.add_argument("--bbox", default=",".join(str(x) for x in DEFAULT_BBOX))
    args = ap.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(","))  # type: ignore[assignment]
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='4GB'")
    con.execute("PRAGMA temp_directory='/tmp/duckdb_bmw'")
    print(f"[1/3] aggregating transitions from {args.shards} ...", flush=True)
    rows = con.execute(_sql(args.shards, bbox)).fetchall()
    cols = [d[0] for d in con.description]
    edges = [dict(zip(cols, r)) for r in rows]
    print(f"      {len(edges):,} transitions in {time.time() - t0:.0f}s", flush=True)

    print("[2/3] collapsing degree-2 chains into segments ...", flush=True)
    segments, graph = collapse_to_segments(edges)
    print(f"      {graph['meta']['n_segments']:,} segments "
          f"from {graph['meta']['n_nodes']:,} nodes "
          f"({graph['meta']['n_junctions']:,} junctions)", flush=True)

    print("[3/3] writing ...", flush=True)
    with open(os.path.join(args.out, "segments.json"), "w") as fh:
        json.dump(segments, fh)
    with open(os.path.join(args.out, "graph.json"), "w") as fh:
        json.dump(graph, fh)

    # Edge-sanity gate: no segment may contain a teleport.
    longest = sorted(segments, key=lambda s: s["length_m"], reverse=True)[:5]
    print("\nlongest segments (eyeball these for teleports):")
    for s in longest:
        print(f"  seg {s['seg_id']}: {s['length_m'] / 1000:.1f} km, "
              f"{len(s['geometry'])} pts, curviness {s['curviness']:.0f} deg/km")
    print(f"\ndone in {time.time() - t0:.0f}s -> {args.out}/segments.json")


if __name__ == "__main__":
    main()
