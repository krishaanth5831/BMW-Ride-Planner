"""Who is this rider, read off their own bike. No questionnaire.

Everything here comes from the telemetry the motorcycle already logs, because
the whiteboard asks for "customer profiling" and "bike type based on speed,
rpm" -- both of which the dataset can answer and neither of which anybody
should have to type in.

What is claimed, and what is NOT: the dataset contains no bike model, no age
and no name, so none is invented. `bike_class` is a coarse family inferred from
how the engine is used, and it is labelled as an inference everywhere it is
shown.
"""

from __future__ import annotations

import csv
import glob
import math
import os
from collections import Counter

from precompute.morton import LEVEL_NODE

# Revs per km/h. A sport bike is geared to sit high in the rev range at road
# speed; a big tourer loafs. Measured over the whole ride, so one hard
# acceleration does not reclassify the bike.
# (*) Bike MODEL is absent from the dataset. These rpm-per-km/h cut-offs, the
# class names, the headroom per class and the experience bands are all ours.
SPORT_RPM_PER_KMH = 60.0       # (*)
ROADSTER_RPM_PER_KMH = 42.0    # (*)

# What the rider's own lean ceiling allows for, by family: a sport bike has
# more angle available than the rider has used, a loaded tourer has less.
BIKE_HEADROOM = {"sport": 1.25, "roadster": 1.15, "tourer / adventure": 1.05}  # (*)

EXPERIENCE = ((0.75, "advanced"), (0.45, "intermediate"), (0.0, "novice"))    # (*)


def _pct(v: list[float], q: float) -> float:
    if not v:
        return 0.0
    s = sorted(v)
    return float(s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))])


def _hav(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def build(folder: str, limit: int = 60) -> dict:
    """Profile one rider from their recorded trips."""
    leans: list[float] = []
    rpms: list[float] = []
    speeds: list[float] = []
    codes: set[str] = set()
    starts: list[tuple[float, float]] = []
    durations: list[float] = []
    gears: list[int] = []
    trips = 0
    total_m = 0.0

    for path in sorted(glob.glob(os.path.join(folder, "*.csv")))[:limit]:
        prev = None
        first = None
        t0 = t1 = None
        moved = 0.0
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    lat = float(row["positionmapmatchedlatitude"])
                    lon = float(row["positionmapmatchedlongitude"])
                    t = float(row["timestampinmillis"])
                except (KeyError, TypeError, ValueError):
                    continue
                if lat == 0.0 and lon == 0.0:
                    continue
                c = (row.get("morton_code") or "").strip()
                if len(c) >= LEVEL_NODE:
                    codes.add(c[:LEVEL_NODE])
                if first is None:
                    first, t0 = (lat, lon), t
                t1 = t

                # Movement from GPS, because `ridingvehiclespeed` is dead for
                # about a quarter of trips -- see build_cell_graph.
                if prev is not None:
                    dt_s = (t - prev[2]) / 1000.0
                    if 0 < dt_s <= 10:
                        d = _hav((prev[0], prev[1]), (lat, lon))
                        v = d / dt_s * 3.6
                        if 0 < v < 200:
                            moved += d
                            if v > 20:
                                speeds.append(v)
                                try:
                                    leans.append(abs(float(row["sensorsbankingangle"])))
                                except (KeyError, TypeError, ValueError):
                                    pass
                                try:
                                    r = float(row["ridingenginespeed"])
                                    if r > 500:
                                        rpms.append(r)
                                except (KeyError, TypeError, ValueError):
                                    pass
                                try:
                                    g = int(float(row["ridinggear"]))
                                    if g > 0:
                                        gears.append(g)
                                except (KeyError, TypeError, ValueError):
                                    pass
                prev = (lat, lon, t)
        if first is None:
            continue
        trips += 1
        total_m += moved
        starts.append(first)
        if t0 and t1 and t1 > t0:
            durations.append((t1 - t0) / 60000.0)

    if not trips:
        return {"available": False}

    lean_p95 = _pct(leans, 0.95)
    rpm_mean = sum(rpms) / len(rpms) if rpms else 0.0
    spd_mean = sum(speeds) / len(speeds) if speeds else 0.0
    rpm_per_kmh = (rpm_mean / spd_mean) if spd_mean else 0.0
    bike = ("sport" if rpm_per_kmh > SPORT_RPM_PER_KMH else
            "roadster" if rpm_per_kmh > ROADSTER_RPM_PER_KMH else
            "tourer / adventure")

    # Home is where rides START, not where the rider spends time: a commuter
    # parked at an office all day would otherwise be "from" the office.
    home = Counter((round(a, 2), round(b, 2)) for a, b in starts).most_common(1)[0][0]

    # Experience from angle used and distance covered, both capped so a single
    # long tour cannot buy a rider an "advanced" badge.
    score = min(1.0, lean_p95 / 42.0) * 0.6 + min(1.0, total_m / 4e6) * 0.4
    level = next(name for gate, name in EXPERIENCE if score >= gate)

    return {
        "available": True,
        "trips": trips,
        "km_total": round(total_m / 1000.0),
        "squares_ridden": len(codes),
        "lean_p95": round(lean_p95, 1),
        "lean_ceiling": round(lean_p95 * BIKE_HEADROOM[bike], 1),
        "speed_mean_kmh": round(spd_mean, 1),
        "rpm_mean": round(rpm_mean),
        "rpm_per_kmh": round(rpm_per_kmh, 1),
        "bike_class": bike,
        "top_gear_seen": max(gears) if gears else None,
        "experience": level,
        "experience_score": round(score, 2),
        "home": {"lat": home[0], "lon": home[1]},
        "typical_ride_min": round(_pct(durations, 0.5)) if durations else None,
        "longest_ride_min": round(_pct(durations, 0.95)) if durations else None,
        "ridden_squares": sorted(codes),
    }
