"""Rider profile from uploaded telemetry.

Three channels, deliberately separate (plan/ALGORITHM.md section 6):

  TASTE       -> KPI weights, fitted from what they over-index on. Overridable.
  CAPABILITY  -> a hard ceiling. NEVER overridable.
  CONTEXT     -> where they start, how long they ride, when. Sets defaults.

Nothing here is claimed that the data does not contain: no age, no bike model.
"""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict

import duckdb

from precompute.build_osm_graph import (ABS_ENGAGED, GRID, LEAN_STEP_NOISE,
                                        LEAN_STRAIGHT, build_index, snap)

# Features the taste model fits weights over. Each must exist per segment.
TASTE_FEATURES = ("curviness", "leaned_share", "speed_mean", "band_share", "elev_mean")

# Safety margin on top of the rider's demonstrated style (section 6, channel 2).
CAPABILITY_MARGIN = 1.15
BIKE_MARGIN = {"sport": 1.25, "roadster": 1.15, "tourer / adventure": 1.05}


def _percentile(values: list[float], q: float) -> float:
    values = sorted(float(v) for v in values if v is not None)
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[index]


def _weighted_percentile(values: list[tuple[float, float]], q: float) -> float:
    values = sorted((float(v), max(float(w), 0.0)) for v, w in values
                    if v is not None and w > 0)
    if not values:
        return 0.0
    target = sum(w for _, w in values) * q
    acc = 0.0
    for value, weight in values:
        acc += weight
        if acc >= target:
            return value
    return values[-1][0]


EXPERIENCE_GATES = (
    (0.4, "novice", 3.0),
    (0.65, "intermediate", 5.0),
    (0.85, "advanced", 8.0),
    (float("inf"), "expert", 10.0),
)


def _experience_level(score: float) -> tuple[str, float]:
    for ceiling, level, alpha_max in EXPERIENCE_GATES:
        if score < ceiling:
            return level, alpha_max
    return "expert", 10.0


WEATHER_FACTOR_DEFAULT = 1.0




def _q(con, sql: str, params=None):
    return con.execute(sql, params or []).fetchall()


def summarise_trips(paths: list[str]) -> dict:
    """Per-trip and per-cell rollups of an uploaded rider's CSVs."""
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='2GB'")
    files = "[" + ",".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"

    sql = f"""
    WITH raw AS (
        SELECT filename AS trip, timestampinmillis AS ts,
               positionmapmatchedlatitude  AS lat,
               positionmapmatchedlongitude AS lon,
               positionmapmatchedheading AS hdg,
               positionrawelevation AS elev, ridingvehiclespeed AS speed,
               sensorsbankingangle AS lean, ridingabsbraking AS absb,
               ridinggear AS gear, ridingenginespeed AS rpm,
               sensorsoutsidetemperature AS temp
        FROM read_csv({files}, filename = true, union_by_name = true,
                      ignore_errors = true)
        WHERE positionmapmatchedlatitude != 0
    ),
    seq AS (
        SELECT *,
            CAST(floor(((lon + 180.0) / 360.0) * {GRID}) AS BIGINT) AS cx,
            CAST(floor(((lat +  90.0) / 360.0) * {GRID}) AS BIGINT) AS cy,
            lag(lat)  OVER w AS p_lat, lag(lon) OVER w AS p_lon,
            lag(lean) OVER w AS p_lean
        FROM raw WINDOW w AS (PARTITION BY trip ORDER BY ts)
    )
    SELECT trip, ts, lat, lon, hdg, elev, speed, lean, absb, gear, rpm, temp,
           cy * {GRID} + cx AS node,
           sqrt(power((lat - p_lat) * 111320.0, 2)
              + power((lon - p_lon) * 111320.0 * cos(radians(lat)), 2)) AS step_m,
           abs(lean - p_lean) AS d_lean
    FROM seq
    """
    con.execute(f"CREATE TABLE pts AS {sql}")

    n_points = _q(con, "SELECT count(*) FROM pts")[0][0]
    if not n_points:
        raise ValueError("no usable rows found in the uploaded files")

    trips = _q(con, """
        SELECT trip,
               min(ts) AS t0, max(ts) AS t1,
               sum(CASE WHEN step_m <= 150 THEN step_m ELSE 0 END) AS dist_m,
               max(abs(lean)) AS lean_max,
               quantile_cont(abs(lean), 0.9) AS lean_p90,
               quantile_cont(abs(lean), 0.95) AS lean_p95,
               sum(CASE WHEN d_lean >= ? THEN d_lean ELSE 0 END) AS lean_delta,
               avg(NULLIF(speed, 0)) AS speed_mean,
               max(speed) AS speed_max,
               max(NULLIF(elev, 0)) AS elev_max,
               avg(NULLIF(temp, 0)) AS temp_mean,
               sum(CASE WHEN absb >= ? THEN 1 ELSE 0 END) AS abs_events,
               count(*) AS n
        FROM pts GROUP BY trip HAVING sum(CASE WHEN step_m <= 150 THEN step_m ELSE 0 END) > 500
        ORDER BY t0
    """, [LEAN_STEP_NOISE, ABS_ENGAGED])
    trip_cols = [d[0] for d in con.description]
    trips = [dict(zip(trip_cols, r)) for r in trips]
    if not trips:
        raise ValueError("uploaded files contain no trip longer than 500 m")

    # Per-cell rollup, used to compare the rider against the crowd on the
    # SAME roads -- that is what makes the style ratio road-normalised.
    cells = _q(con, """
        SELECT node, avg(lat) AS lat, avg(lon) AS lon, avg(hdg) AS hdg,
               sum(CASE WHEN step_m <= 150 THEN step_m ELSE 0 END) AS path_m,
               sum(CASE WHEN d_lean >= ? THEN d_lean ELSE 0 END) AS lean_delta,
               sum(CASE WHEN abs(lean) > ? THEN step_m ELSE 0 END) AS leaned_m,
               quantile_cont(abs(lean), 0.95) AS lean_p95,
               avg(NULLIF(speed, 0)) AS speed_mean,
               avg(CASE WHEN speed BETWEEN 50 AND 120 THEN 1.0 ELSE 0.0 END) AS band_share,
               avg(NULLIF(elev, 0)) AS elev_mean,
               count(*) AS n
        FROM pts GROUP BY node
    """, [LEAN_STEP_NOISE, LEAN_STRAIGHT])
    cell_cols = [d[0] for d in con.description]
    cells = {r[0]: dict(zip(cell_cols, r)) for r in cells}

    starts = _q(con, """
        SELECT lat, lon, ts FROM (
            SELECT lat, lon, ts, row_number() OVER (PARTITION BY trip ORDER BY ts) AS rn
            FROM pts) WHERE rn = 1
    """)

    overall = _q(con, """
        SELECT quantile_cont(abs(lean), 0.90), quantile_cont(abs(lean), 0.95),
               quantile_cont(abs(lean), 0.99), avg(NULLIF(rpm,0)),
               avg(NULLIF(speed,0)), max(NULLIF(gear,0))
        FROM pts
    """)[0]

    con.close()
    return {
        "n_points": n_points,
        "trips": trips,
        "cells": cells,
        "starts": starts,
        "lean_p90": overall[0] or 0.0,
        "lean_p95": overall[1] or 0.0,
        "lean_p99": overall[2] or 0.0,
        "rpm_mean": overall[3] or 0.0,
        "speed_mean": overall[4] or 0.0,
        "max_gear": int(overall[5] or 0),
    }


def _crowd_baseline(segments: list[dict]) -> dict:
    """Mean and sd of each taste feature across the crowd, distance-weighted."""
    stats = {}
    for f in TASTE_FEATURES:
        vals = [(s[f], s["length_m"]) for s in segments if s.get(f)]
        if not vals:
            stats[f] = (0.0, 1.0)
            continue
        wsum = sum(w for _, w in vals)
        mu = sum(v * w for v, w in vals) / wsum
        var = sum(((v - mu) ** 2) * w for v, w in vals) / wsum
        stats[f] = (mu, math.sqrt(var) or 1.0)
    return stats


def build_profile(paths: list[str], segments: list[dict], name: str = "uploaded",
                  index: dict | None = None,
                  weather_factor: float = WEATHER_FACTOR_DEFAULT) -> dict:
    """The full three-channel profile."""
    summary = summarise_trips(paths)
    baseline = _crowd_baseline(segments)

    # Rider cells are morton cells; OSM segment endpoints are OSM nodes. There
    # is no id in common, so map the rider onto the road network the same way
    # the crowd was mapped: snap each cell to the nearest segment by distance
    # and heading.
    if index is None:
        index = build_index(segments)
    seg_by_id = {s["seg_id"]: s for s in segments}
    by_node: dict[int, dict] = {}
    for node, c in summary["cells"].items():
        lat, lon = c.get("lat"), c.get("lon")
        if not lat:
            continue
        sid, _d = snap((lat, lon), c.get("hdg"), segments, index)
        if sid is not None:
            by_node[node] = seg_by_id[sid]

    # ---- Channel 1: TASTE -------------------------------------------------
    # Revealed preference: score the segments the rider CHOSE, using each
    # segment's own crowd-measured features, weighted by how far they rode on
    # it. Comparing rider-side cell aggregates against crowd-side segment
    # aggregates would be a scale mismatch -- cell-level variance is far higher
    # than segment-level -- so both sides are measured on the same objects.
    rider_dist: dict[int, float] = defaultdict(float)
    for node, c in summary["cells"].items():
        seg = by_node.get(node)
        if seg is not None:
            rider_dist[seg["seg_id"]] += float(c["path_m"] or 0.0)

    seg_by_id = {s["seg_id"]: s for s in segments}
    rider_mean: dict[str, float] = {}
    total_w = sum(rider_dist.values())
    for f in TASTE_FEATURES:
        if total_w <= 0:
            rider_mean[f] = 0.0
            continue
        acc = 0.0
        for sid, w in rider_dist.items():
            v = seg_by_id[sid].get(f)
            if v:
                acc += float(v) * w
        rider_mean[f] = acc / total_w

    z = {}
    for f in TASTE_FEATURES:
        mu, sd = baseline[f]
        z[f] = (rider_mean[f] - mu) / sd if sd else 0.0

    # Softmax over the z-scores rather than clip-and-normalise. Clipping made
    # the weights collapse onto whichever single feature happened to be the only
    # positive one, which is not a preference -- it is an artefact.
    import math as _m
    TEMP = 0.6                      # lower = sharper preferences
    exp = {f: _m.exp(min(max(z[f], -4.0), 4.0) / TEMP) for f in TASTE_FEATURES}
    tot = sum(exp.values())
    weights = {f: v / tot for f, v in exp.items()}
    overlap_m = total_w

    dists = [(t["dist_m"] or 0) / 1000.0 for t in summary["trips"]]

    # ---- Rider experience --------------------------------------------------
    # Experience is deliberately a conservative blend of exposure, lean and
    # road-normalised curviness. It gates the search envelope; it is not a
    # preference and never substitutes for the hard capability exclusion below.
    crowd_curviness_p90 = _percentile(
        [float(s.get("curviness") or 0.0) for s in segments], 0.90)
    rider_curviness_p90 = _weighted_percentile(
        [(seg_by_id[sid].get("curviness") or 0.0, weight)
         for sid, weight in rider_dist.items()], 0.90)
    crowd_curviness_p90 = max(crowd_curviness_p90, 1e-6)
    exp_score = (
        0.3 * math.log1p(sum(dists) / 500.0)
        + 0.3 * math.log1p(len(summary["trips"]) / 20.0)
        + 0.2 * (float(summary["lean_p95"] or 0.0) / 35.0)
        + 0.2 * (rider_curviness_p90 / crowd_curviness_p90)
    )
    experience_level, alpha_max = _experience_level(exp_score)

    # ---- Channel 2: CAPABILITY -------------------------------------------
    # Raw lean angle conflates road with rider: a cautious rider on a pass
    # out-leans a fast rider on a motorway. So normalise by the road -- compare
    # the rider's lean to the crowd's lean on the SAME cells.
    ratios = []
    for node, c in summary["cells"].items():
        seg = by_node.get(node)
        if not seg or not seg.get("lean_p95") or (c["n"] or 0) < 5:
            continue
        if seg["lean_p95"] < 5.0:
            continue  # straight road tells us nothing about style
        ratios.append((c["lean_p95"] or 0.0) / seg["lean_p95"])
    ratios.sort()
    if ratios:
        style_ratio = ratios[len(ratios) // 2]
        style_basis = len(ratios)
    else:
        # No overlap with the crowd -> no road-normalised ratio exists. Fall back
        # to the conservative prior and say so, rather than guessing.
        style_ratio = 0.9
        style_basis = 0

    # ---- Channel 3: CONTEXT ----------------------------------------------
    import datetime as _dt

    durations = [
        (t["t1"] - t["t0"]) / 60000.0 for t in summary["trips"] if t["t1"] and t["t0"]
    ]
    durations.sort()
    median_minutes = durations[len(durations) // 2] if durations else 90.0
    median_minutes = float(min(max(median_minutes, 30.0), 300.0))

    hours = Counter()
    for _lat, _lon, ts in summary["starts"]:
        hours[_dt.datetime.utcfromtimestamp(ts / 1000).hour] += 1
    usual_hour = hours.most_common(1)[0][0] if hours else 10

    # Origin = where they actually set off from most often. That is why this
    # needs no user input: the start point comes out of their own data.
    clusters: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for lat, lon, _ts in summary["starts"]:
        clusters[(round(lat, 2), round(lon, 2))].append((lat, lon))
    best = max(clusters.values(), key=len) if clusters else [(48.14, 11.58)]
    origin = (sum(p[0] for p in best) / len(best), sum(p[1] for p in best) / len(best))

    temps = [t["temp_mean"] for t in summary["trips"] if t["temp_mean"]]
    # Coarse bike class. Bike MODEL is not in the dataset, so this is an
    # inference the rider is expected to confirm in the UI.
    rpm, spd = summary["rpm_mean"], summary["speed_mean"]
    rpm_per_kmh = (rpm / spd) if spd else 0.0
    if rpm_per_kmh > 60:
        bike_class = "sport"
    elif rpm_per_kmh > 42:
        bike_class = "roadster"
    else:
        bike_class = "tourer / adventure"
    bike_margin = BIKE_MARGIN[bike_class]
    lean_ceiling = (float(summary["lean_p95"] or 0.0) * bike_margin
                    / max(style_ratio, 0.3) * float(weather_factor))

    return {
        "name": name,
        "n_trips": len(summary["trips"]),
        "n_points": summary["n_points"],
        "taste": {
            "weights": weights,
            "overlap_km": round(overlap_m / 1000.0, 1),
            "z": z,
            "rider_mean": rider_mean,
            "crowd_mean": {f: baseline[f][0] for f in TASTE_FEATURES},
        },
        "experience_level": experience_level,
        "experience_score": round(exp_score, 3),
        "alpha_max": alpha_max,
        "lean_ceiling": round(lean_ceiling, 2),
        "capability": {
            "lean_p90": summary["lean_p90"],
            "lean_p95": summary["lean_p95"],
            "lean_p99": summary["lean_p99"],
            "style_ratio": style_ratio,
            "style_basis_cells": style_basis,
            "margin": bike_margin,
            "lean_ceiling": round(lean_ceiling, 2),
        },
        "context": {
            "origin": {"lat": origin[0], "lon": origin[1]},
            "median_ride_minutes": median_minutes,
            "usual_start_hour": usual_hour,
            "total_km": round(sum(dists), 1),
            "longest_ride_km": round(max(dists), 1) if dists else 0.0,
            "temp_mean_c": round(sum(temps) / len(temps), 1) if temps else None,
            "bike_class": bike_class,
            "rpm_per_kmh": round(rpm_per_kmh, 1),
        },
        "records": {
            "max_lean_deg": round(max((t["lean_max"] or 0) for t in summary["trips"]), 1),
            "max_speed_kmh": round(max((t["speed_max"] or 0) for t in summary["trips"]), 1),
            "max_altitude_m": round(max((t["elev_max"] or 0) for t in summary["trips"]), 0),
            "longest_ride_km": round(max(dists), 1) if dists else 0.0,
        },
        "_cells": summary["cells"],
    }
