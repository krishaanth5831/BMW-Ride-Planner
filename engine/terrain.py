"""Terrain and corner statistics for one selected ride.

Corner counts come from the routed OSM geometry and therefore work offline.
Elevation is sampled only after a route wins, using Open-Meteo's 90 m
Copernicus DEM endpoint. Failure is deliberately non-fatal.
"""

from __future__ import annotations

import json
import math
import statistics
import urllib.parse
import urllib.request
from functools import lru_cache

from engine.net import https_context

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
EARTH_M = 6_371_000.0
TURN_SAMPLE_M = 45.0
SHARP_TURN_DEG = 50.0
HAIRPIN_DEG = 115.0
TURN_SEPARATION_M = 180.0
MAX_ELEVATION_SAMPLES = 160
ELEVATION_BATCH = 80


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _turn_angle(a: float, b: float) -> float:
    return abs((b - a + 180.0) % 360.0 - 180.0)


def _sample(coords: list[list[float]], count: int | None = None,
            spacing_m: float | None = None) -> tuple[list[tuple[float, float]], float]:
    points = [(float(p[0]), float(p[1])) for p in coords]
    if len(points) < 2:
        return points, 0.0
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + _distance(a, b))
    total = cumulative[-1]
    if total <= 0:
        return [points[0]], 0.0
    if count is None:
        count = max(2, int(total / max(spacing_m or TURN_SAMPLE_M, 1.0)) + 1)
    count = max(2, count)
    targets = [total * i / (count - 1) for i in range(count)]
    sampled = []
    segment = 1
    for target in targets:
        while segment < len(cumulative) - 1 and cumulative[segment] < target:
            segment += 1
        a, b = points[segment - 1], points[segment]
        length = cumulative[segment] - cumulative[segment - 1]
        part = 0.0 if length <= 0 else (target - cumulative[segment - 1]) / length
        sampled.append((a[0] + (b[0] - a[0]) * part,
                        a[1] + (b[1] - a[1]) * part))
    return sampled, total


def road_geometry_stats(coords: list[list[float]]) -> dict:
    """Count sustained direction changes, suppressing adjacent geometry points."""
    points, total_m = _sample(coords, spacing_m=TURN_SAMPLE_M)
    look = 3  # compare approximately 135 m of approach with 135 m of exit
    candidates = []
    for i in range(look, len(points) - look):
        angle = _turn_angle(_bearing(points[i - look], points[i]),
                            _bearing(points[i], points[i + look]))
        if angle >= SHARP_TURN_DEG:
            candidates.append((angle, i))
    # Keep the strongest point in each bend instead of counting one curve at
    # every resampled coordinate.
    selected: list[tuple[float, int]] = []
    separation = max(1, round(TURN_SEPARATION_M / TURN_SAMPLE_M))
    for angle, index in sorted(candidates, reverse=True):
        if all(abs(index - old_index) > separation for _, old_index in selected):
            selected.append((angle, index))
    sharp = len(selected)
    hairpins = sum(angle >= HAIRPIN_DEG for angle, _ in selected)
    km = total_m / 1000.0
    return {
        "sharp_turns": sharp,
        "hairpin_turns": hairpins,
        "curves_per_10km": round(sharp * 10.0 / max(km, .1), 1),
        "sharpest_turn_deg": round(max((a for a, _ in selected), default=0.0)),
        "turn_analysis_source": "calculated from routed OSM road geometry",
    }


@lru_cache(maxsize=256)
def _fetch_elevation_batch(points: tuple[tuple[float, float], ...]) -> tuple[float, ...]:
    params = urllib.parse.urlencode({
        "latitude": ",".join(f"{p[0]:.6f}" for p in points),
        "longitude": ",".join(f"{p[1]:.6f}" for p in points),
    }, safe=",")
    request = urllib.request.Request(
        f"{ELEVATION_URL}?{params}",
        headers={"User-Agent": "BMW-Ride-Planner lightweight demo"})
    with urllib.request.urlopen(request, timeout=8,
                                context=https_context()) as response:
        payload = json.load(response)
    values = payload.get("elevation") or []
    if len(values) != len(points):
        raise ValueError("elevation response length did not match route samples")
    return tuple(float(value) for value in values)


def elevation_stats(coords: list[list[float]]) -> dict:
    points, total_m = _sample(
        coords, count=min(MAX_ELEVATION_SAMPLES,
                          max(2, int(sum(_distance((a[0], a[1]), (b[0], b[1]))
                                             for a, b in zip(coords, coords[1:]))
                                     / 500.0) + 1)))
    elevations = []
    for start in range(0, len(points), ELEVATION_BATCH):
        elevations.extend(_fetch_elevation_batch(tuple(points[start:start + ELEVATION_BATCH])))
    # A small median filter removes single-cell DEM spikes before accumulating
    # ascent. Changes under two metres are treated as raster noise.
    smooth = [statistics.median(elevations[max(0, i - 1):i + 2])
              for i in range(len(elevations))]
    gain = sum(delta for a, b in zip(smooth, smooth[1:])
               if (delta := b - a) > 2.0)
    loss = sum(-delta for a, b in zip(smooth, smooth[1:])
               if (delta := b - a) < -2.0)
    return {
        "elevation_available": True,
        "elev_gain_m": round(gain),
        "elev_loss_m": round(loss),
        "elevation_min_m": round(min(smooth)),
        "elevation_max_m": round(max(smooth)),
        "elevation_samples": len(smooth),
        "elevation_resolution_m": 90,
        "elevation_source": "Open-Meteo · Copernicus DEM",
        "route_sample_spacing_m": round(total_m / max(len(smooth) - 1, 1)),
    }


def enrich_route(route: dict, fetch_elevation: bool = True) -> dict:
    kpis = route.setdefault("kpis", {})
    kpis.update(road_geometry_stats(route.get("coords") or []))
    if not fetch_elevation:
        kpis["elevation_available"] = False
        return route
    try:
        kpis.update(elevation_stats(route.get("coords") or []))
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
        kpis["elevation_available"] = False
        kpis["elevation_source"] = "temporarily unavailable"
    return route
