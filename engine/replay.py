"""The ride you actually did, read back off the bike.

Nothing here plans anything. It reads one recorded trip out of the dataset and
reports what the motorcycle logged, which is the point: every number below was
MEASURED. Lean angle in particular is the one a phone cannot give you, so a
post-ride summary built on it is something only the bike can produce.

Kept deliberately separate from the planner. It shares no state with it and is
never consulted when building a route.
"""

from __future__ import annotations

import csv
import glob
import math
import os

# (*) Above this the rider is committed into a bend rather than just steering.
LEANED_DEG = 15.0

# (*) Longitudinal acceleration is logged in g. Calibrated against this
# dataset rather than guessed: -0.25 g fires on 9.5% of all decelerating
# samples, which is ordinary riding, and -0.35 g on 2.9%. -0.5 g fires on
# 0.44% and is a genuinely firm stop. The brake-pressure channels are dead in
# these trips -- every row reads 0.0 -- so this is the only braking signal
# there is, and it must not be over-sensitive.
HARD_DECEL_G = -0.50

# (*) Two hard-braking samples inside this many seconds are one event, not two.
BRAKE_EVENT_GAP_S = 3.0

# (*) Rejects GPS spikes. Nothing in this dataset legitimately exceeds it.
MAX_PLAUSIBLE_KMH = 220.0

# Points kept for the drawn outline. More than this adds nothing at thumbnail
# size and makes the payload silly.
TRACE_POINTS = 600


def _f(row: dict, key: str):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def haversine_m(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def list_trips(folder: str) -> list[str]:
    return sorted(glob.glob(os.path.join(folder, "*.csv")))


def summarise(path: str) -> dict:
    """Everything the bike recorded on one ride."""
    pts: list[tuple[float, float]] = []
    elev: list[float] = []
    leans: list[float] = []
    speeds: list[float] = []
    lean_delta = 0.0
    leaned_s = 0.0
    moving_s = 0.0
    dist_m = 0.0
    brake_events = 0
    last_brake_t = -1e9
    t_first = t_last = None
    prev = None
    prev_lean = None

    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            lat, lon = (_f(row, "positionmapmatchedlatitude"),
                        _f(row, "positionmapmatchedlongitude"))
            t = _f(row, "timestampinmillis")
            if lat is None or lon is None or t is None:
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            if t_first is None:
                t_first = t
            t_last = t

            v = None
            if prev is not None:
                dt = (t - prev[2]) / 1000.0
                if 0 < dt <= 10:
                    d = haversine_m((prev[0], prev[1]), (lat, lon))
                    v = d / dt * 3.6
                    if v > MAX_PLAUSIBLE_KMH:
                        v = None
                    else:
                        dist_m += d
                        if v > 5:
                            moving_s += dt
            prev = (lat, lon, t)

            lean = abs(_f(row, "sensorsbankingangle") or 0.0)
            # Only count lean while moving: a bike on its stand reads an angle.
            if v is not None and v > 15:
                leans.append(lean)
                speeds.append(v)
                if prev_lean is not None:
                    lean_delta += abs(lean - prev_lean)
                prev_lean = lean
                if lean >= LEANED_DEG:
                    leaned_s += 1.0     # one sample; scaled by rate below

            decel = _f(row, "sensorsaccelerationlongitudinal")
            if decel is not None and decel < HARD_DECEL_G:
                if (t - last_brake_t) / 1000.0 > BRAKE_EVENT_GAP_S:
                    brake_events += 1
                last_brake_t = t

            e = _f(row, "positionmapmatchedelevation")
            if e and e > 0:
                elev.append(e)
            pts.append((lat, lon))

    if not pts or t_first is None or dist_m < 500:
        return {}

    duration_s = (t_last - t_first) / 1000.0
    # Sample rate varies between trips, so convert the leaned SAMPLE count into
    # seconds using this trip's own rate rather than assuming 1 Hz.
    rate = len(leans) / max(moving_s, 1e-6) if moving_s else 1.0
    leaned_seconds = leaned_s / max(rate, 1e-6)

    # Climb off a smoothed profile: raw GPS altitude noise invents thousands of
    # fictitious metres over a few thousand samples.
    win = 15
    sm = [sum(elev[max(0, i - win // 2):i + win // 2 + 1])
          / len(elev[max(0, i - win // 2):i + win // 2 + 1])
          for i in range(len(elev))] if len(elev) >= win else elev
    climb = sum(d for i in range(1, len(sm)) if (d := sm[i] - sm[i - 1]) > 1.0)

    step = max(1, len(pts) // TRACE_POINTS)
    trace = [[round(a, 5), round(b, 5)] for a, b in pts[::step]]

    km = dist_m / 1000.0
    return {
        "trip_id": os.path.splitext(os.path.basename(path))[0],
        "km": round(km, 1),
        "minutes": round(duration_s / 60.0),
        "moving_minutes": round(moving_s / 60.0),
        "max_lean_deg": round(max(leans), 1) if leans else 0.0,
        "lean_p95_deg": round(sorted(leans)[int(.95 * (len(leans) - 1))], 1)
                        if leans else 0.0,
        "leaned_minutes": round(leaned_seconds / 60.0, 1),
        "leaned_pct": round(100.0 * leaned_seconds / max(moving_s, 1e-6)),
        "curviness": round(lean_delta / max(km, 1e-6)),
        # Sustained, not a single sample: one bad GPS fix otherwise reports a
        # 207 km/h top speed off a 3-second glitch.
        "top_speed_kmh": round(_sustained_max(speeds)),
        "avg_moving_kmh": round(dist_m / max(moving_s, 1e-6) * 3.6),
        "hard_braking": brake_events,
        "climb_m": round(climb),
        "trace": trace,
        "start": trace[0] if trace else None,
        "finish": trace[-1] if trace else None,
    }


def _sustained_max(speeds: list[float], window: int = 5) -> float:
    """Fastest rolling mean, so a lone GPS spike cannot set the record."""
    if not speeds:
        return 0.0
    if len(speeds) < window:
        return max(speeds)
    run = sum(speeds[:window])
    best = run
    for i in range(window, len(speeds)):
        run += speeds[i] - speeds[i - window]
        best = max(best, run)
    return best / window


def best_trip(folder: str, limit: int = 40) -> dict:
    """The most worth-showing ride this rider recorded.

    Ranked on what makes a summary interesting to look at rather than on
    distance alone: a long motorway drone is a worse story than an hour in the
    mountains, so lean and climb carry most of the weight.
    """
    best = {}
    best_score = -1.0
    for path in list_trips(folder)[:limit]:
        s = summarise(path)
        if not s:
            continue
        score = (s["max_lean_deg"] / 45.0 * 0.4
                 + min(s["climb_m"], 1200) / 1200.0 * 0.3
                 + min(s["km"], 150) / 150.0 * 0.2
                 + min(s["curviness"], 400) / 400.0 * 0.1)
        if score > best_score:
            best, best_score = s, score
    return best
