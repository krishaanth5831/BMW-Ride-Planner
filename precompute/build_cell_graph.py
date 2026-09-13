"""Stage (1)+(2) of the ORIGINAL algorithm: the crowd IS the road network.

plan/ALGORITHM.md at b38f8d4, sections 3 and 4. No OSM download anywhere in
here -- nodes are level-18 morton squares that riders have actually ridden, and
edges are observed square->square transitions carrying the median speed riders
actually held across them.

Two filters are not optional (section 4):

  1. drop transitions longer than MAX_GAP_M -- a 30 s GPS dropout otherwise
     becomes a 2 km "free shortcut" that Dijkstra will find and love. BMW's own
     viewer guards this at tripViewer app.js:176.
  2. require >= MIN_TRIPS distinct trips per edge -- one rider's stray sample
     must not create a road, and this is also what prunes parallel-road
     collapse when a motorway and its frontage road land in the same square.
"""

from __future__ import annotations

import csv
import argparse
import datetime as dt
import json
import math
import os
import sys
from array import array
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from precompute.morton import LEVEL_NODE, cell_center, morton_code  # noqa: E402

# 1.5x the level-18 cell diagonal, per section 4.
MAX_GAP_M = 150.0   # (*) GPS-dropout cutoff, from cell geometry
MIN_TRIPS = 2       # (*) trips needed before an edge is trusted

# Percentiles need samples, and there are ~1M squares. Keep a small reservoir
# per square instead of every value: 32 samples pins p50/p90/p95 closely enough
# for a cost multiplier and keeps the whole pass in memory.
RESERVOIR = 32      # (*) sampling size, implementation detail

# Region gate. The crowd data is Bavaria-centric; bounding it keeps the square
# count finite and the aggregation honest about where we actually have riders.
BBOX = (47.0, 9.8, 49.4, 13.4)   # (*) our chosen region; south, west, north, east


def _f(row: dict, key: str):
    try:
        v = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def pct(values, q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return float(s[i])


class Cell:
    """One ~92 m square that riders have ridden. This is a graph NODE."""

    __slots__ = ("n", "trips", "lean", "speed", "elev_sum", "abs_n", "decel_n",
                 "throttle_sum", "path_m", "elev_n")

    def __init__(self):
        self.n = 0
        self.trips: set[str] = set()
        self.lean = array("f")
        self.speed = array("f")
        self.elev_sum = 0.0
        self.elev_n = 0
        self.abs_n = 0
        self.decel_n = 0
        self.throttle_sum = 0.0
        self.path_m = 0.0

    def add(self, lean, speed, elev, abs_on, decel, throttle):
        self.n += 1
        if len(self.lean) < RESERVOIR:
            self.lean.append(lean)
        if speed and len(self.speed) < RESERVOIR:
            self.speed.append(speed)
        if elev is not None:
            self.elev_sum += elev
            self.elev_n += 1
        self.abs_n += abs_on
        self.decel_n += decel
        self.throttle_sum += throttle


class Edge:
    """An observed square->square transition. This is a graph EDGE."""

    __slots__ = ("trips", "dist", "secs")   # dist = RAW sample gap, for Filter 1

    def __init__(self):
        self.trips: set[str] = set()
        self.dist = array("f")
        self.secs = array("f")


def ingest(paths: list[str], progress_every: int = 200):
    cells: dict[str, Cell] = {}
    edges: dict[tuple[str, str], Edge] = {}
    stats = defaultdict(int)

    for i, path in enumerate(paths):
        if i % progress_every == 0:
            print(f"  [{i}/{len(paths)}] {len(cells)} squares, {len(edges)} raw edges",
                  flush=True)
        try:
            fh = open(path, newline="")
        except OSError:
            continue
        with fh:
            prev = None     # (code, lat, lon, t_ms) at the last square CHANGE
            prev_s = None   # (lat, lon, t_ms) at the previous SAMPLE
            for row in csv.DictReader(fh):
                lat = _f(row, "positionmapmatchedlatitude")
                lon = _f(row, "positionmapmatchedlongitude")
                if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
                    continue
                if not (BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]):
                    stats["out_of_region"] += 1
                    continue
                trip = row.get("trip_id") or os.path.basename(path)
                t_ms = _f(row, "timestampinmillis")

                # The dataset ships the authoritative code; trust its prefix and
                # fall back to our own encoder only if the column is missing.
                truth = (row.get("morton_code") or "").strip()
                code = truth[:LEVEL_NODE] if len(truth) >= LEVEL_NODE \
                    else morton_code(lat, lon, LEVEL_NODE)

                cell = cells.get(code)
                if cell is None:
                    cell = cells[code] = Cell()
                # `ridingvehiclespeed` is dead for roughly a quarter of trips
                # -- every row reads 0.0. Falling back to GPS keeps those trips
                # from silently contributing no speed at all, which would leave
                # whole regions priced off the crowd's slowest riders.
                speed = _f(row, "ridingvehiclespeed") or 0.0
                if speed <= 0 and prev_s is not None and t_ms:
                    dt_s = (t_ms - prev_s[2]) / 1000.0
                    if 0 < dt_s <= 10:
                        v = haversine_m((prev_s[0], prev_s[1]), (lat, lon)) / dt_s * 3.6
                        speed = v if 0 < v < 200 else 0.0
                prev_s = (lat, lon, t_ms)
                lean = abs(_f(row, "sensorsbankingangle") or 0.0)
                lon_acc = _f(row, "sensorsaccelerationlongitudinal") or 0.0
                # `ridingabsbraking` reads 1 on essentially every row -- it is
                # an ABS-available flag, not an intervention count, so it
                # carries no signal. Brake pressure does.
                brake = max(_f(row, "sensorsbreakpressurefront") or 0.0,
                            _f(row, "sensorsbreakpressurerear") or 0.0)
                # Longitudinal acceleration is logged in g, so the hard-decel
                # threshold is -0.25 g, not -3 m/s2.
                elev = _f(row, "positionmapmatchedelevation")
                cell.add(lean, speed, elev if elev and elev > 0 else None,
                         1 if brake > 0.5 else 0,
                         1 if lon_acc < -0.25 else 0,
                         _f(row, "ridingthrottlevalue") or 0.0)
                cell.trips.add(trip)
                stats["samples"] += 1

                if prev is not None and prev[0] != code and prev[3] and t_ms:
                    d = haversine_m((prev[1], prev[2]), (lat, lon))
                    dt = (t_ms - prev[3]) / 1000.0
                    if d > MAX_GAP_M:
                        stats["dropped_teleport"] += 1      # Filter 1
                    elif dt <= 0 or dt > 60:
                        stats["dropped_time"] += 1
                    else:
                        key = (prev[0], code)
                        e = edges.get(key)
                        if e is None:
                            e = edges[key] = Edge()
                        e.trips.add(trip)
                        if len(e.dist) < RESERVOIR:
                            e.dist.append(d)
                            e.secs.append(dt)
                        cells[prev[0]].path_m += d
                if prev is None or prev[0] != code:
                    prev = (code, lat, lon, t_ms)
                else:
                    prev = (code, lat, lon, t_ms)
        stats["trips"] += 1
    return cells, edges, stats


def finalise(cells: dict[str, Cell], edges: dict[tuple[str, str], Edge], stats):
    """Apply Filter 2, then emit the routable graph."""
    kept = {k: e for k, e in edges.items() if len(e.trips) >= MIN_TRIPS}
    stats["dropped_one_trip"] = len(edges) - len(kept)

    live = set()
    for a, b in kept:
        live.add(a)
        live.add(b)

    # Regional fallbacks for the documented speed chain (section 8, property 4).
    all_speeds = [s for c in cells.values() for s in c.speed if s > 0]
    region_speed = pct(all_speeds, 0.5) or 40.0

    out_cells = {}
    for code in live:
        c = cells[code]
        speeds = [s for s in c.speed if s > 0]
        lat, lon = cell_center(code)
        out_cells[code] = {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "n": c.n,
            "n_trips": len(c.trips),
            "lean_p50": round(pct(c.lean, 0.50), 2),
            "lean_p90": round(pct(c.lean, 0.90), 2),
            "lean_p95": round(pct(c.lean, 0.95), 2),
            "speed_med": round(pct(speeds, 0.50) if speeds else region_speed, 2),
            "speed_p85": round(pct(speeds, 0.85) if speeds else region_speed, 2),
            "elev_m": round(c.elev_sum / c.elev_n, 1) if c.elev_n else None,
            "abs_rate": round(c.abs_n / max(c.n, 1), 4),
            "decel_rate": round(c.decel_n / max(c.n, 1), 4),
            "throttle": round(c.throttle_sum / max(c.n, 1), 2),
        }

    out_edges = []
    for (a, b), e in kept.items():
        gap = sum(e.dist) / len(e.dist)
        t = sum(e.secs) / len(e.secs)
        # The RAW gap between the two GPS samples that straddled the boundary
        # is metres apart at 1 Hz -- it is the teleport guard, not a length.
        # What the rider actually covers crossing a square is centre to centre.
        d = haversine_m((out_cells[a]["lat"], out_cells[a]["lon"]),
                        (out_cells[b]["lat"], out_cells[b]["lon"]))
        # Documented speed chain (section 8, property 4): the bike's OWN
        # reported speed across the two squares first, then the derived d/t,
        # then the regional median. Never let a missing v produce a
        # divide-by-zero shortcut. d/t is unreliable on its own here because
        # the log is irregularly sampled, so a sub-second gap inflates it.
        ends = [out_cells[a]["speed_p85"], out_cells[b]["speed_p85"]]
        ends = [s for s in ends if s > 0]
        if ends:
            v = sum(ends) / len(ends)
        elif t > 0:
            v = (d / t) * 3.6
        else:
            v = region_speed
        out_edges.append({
            "a": a, "b": b,
            "len_m": round(d, 1),
            "gap_m": round(gap, 1),
            "v_kmh": round(max(3.0, min(200.0, v)), 1),
            "n_trips": len(e.trips),
        })

    return {"level": LEVEL_NODE, "cells": out_cells, "edges": out_edges,
            "region_speed_kmh": round(region_speed, 1),
            "stats": dict(stats)}


def verify(graph: dict) -> None:
    """Section 4: assert the graph before trusting it."""
    bad_len = [e for e in graph["edges"] if e["gap_m"] > MAX_GAP_M]
    bad_trips = [e for e in graph["edges"] if e["n_trips"] < MIN_TRIPS]
    assert not bad_len, f"{len(bad_len)} edges exceed MAX_GAP_M -- teleports survived"
    assert not bad_trips, f"{len(bad_trips)} edges below MIN_TRIPS"
    longest = sorted(graph["edges"], key=lambda e: -e["gap_m"])[:20]
    print(f"Graph gate OK: {len(graph['cells'])} squares, {len(graph['edges'])} edges")
    print("  20 longest edges (eyeball these on a map before demoing):")
    for e in longest[:5]:
        c = graph["cells"][e["a"]]
        print(f"    {e['gap_m']:6.1f} m gap  {e['v_kmh']:5.1f} km/h  "
              f"{e['n_trips']:3d} trips  @ {c['lat']:.4f},{c['lon']:.4f}")


def collect_paths(roots: list[str], limit: int | None = None) -> list[str]:
    out = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            if "__MACOSX" in dirpath:
                continue
            for f in sorted(files):
                if f.endswith(".csv"):
                    out.append(os.path.join(dirpath, f))
    out = sorted(set(os.path.realpath(path) for path in out))
    return out[:limit] if limit else out


def main(argv: list[str]) -> int:
    """Scan the complete configured corpus once, outside the server request path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", help="Explicit trip directories (recursive)")
    parser.add_argument("--dataset", default=os.environ.get(
        "BMW_DATASET", "~/Desktop/BMW/exd_download/datasetHackathon"))
    parser.add_argument("--out", default="data", help="Directory for cell_graph.json")
    parser.add_argument("--limit", type=int, help="Explicit development-only file limit; omitted = all files")
    args = parser.parse_args(argv[1:])
    roots = [os.path.abspath(os.path.expanduser(r)) for r in (args.roots or [args.dataset])]
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        parser.error("Dataset directory not found: " + ", ".join(missing))
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    paths = collect_paths(roots, args.limit)
    if not paths:
        parser.error("No CSV trips found. Existing graph has not been changed.")
    print(f"Ingesting {len(paths)} trip files from {len(roots)} sources...", flush=True)

    cells, edges, stats = ingest(paths)
    graph = finalise(cells, edges, stats)
    verify(graph)
    if not graph["cells"] or not graph["edges"]:
        parser.error("No shared in-region road transitions survived validation; "
                     "existing graph has not been changed.")
    graph["source"] = {
        "kind": "BMW CSV telemetry", "files_processed": len(paths),
        "roots": roots, "file_limit": args.limit, "region_bbox": list(BBOX),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "aggregation": "existing per-cell capped samples; all selected files scanned",
    }
    from engine.cache import write_json
    output = os.path.join(args.out, "cell_graph.json")
    write_json(output, graph)
    size = os.path.getsize(output) / 1e6
    print(f"Wrote {output} ({size:.1f} MB)")
    print("  stats:", json.dumps(graph["stats"], indent=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
