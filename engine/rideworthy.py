"""When is it worth going out? The forecast, read as ride windows.

The whiteboard's first line: "on good weather it sends a notification". That
needs the question turned around. A forecast answers "what is the weather at
3pm"; a rider wants "when is the next stretch long enough to be worth getting
the bike out, and how long have I got".

So: pull the hourly forecast, score every hour for RIDING specifically, and
merge the good hours into contiguous windows. A window has to be long enough
to be worth it -- an hour of sunshine between two showers is not a ride.

Scored on what actually stops a motorcyclist, in order of how much it matters:
rain first (it is the only one that ends a ride outright), then cold hands,
then gusts, then daylight. Nothing here is a generic "nice day" score; 24 C and
gusting 60 km/h is a lovely day and a poor ride.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "fixtures", "weather")
TIMEOUT_S = 8

# Degrees C. Below IDEAL_LO hands get cold through summer gloves; above
# IDEAL_HI full textile kit becomes unpleasant in traffic.
IDEAL_LO, IDEAL_HI = 14.0, 27.0
COLD_FLOOR, HOT_CEIL = 4.0, 35.0

GUST_OK, GUST_BAD = 25.0, 55.0          # km/h
MIN_WINDOW_H = 2                        # shorter than this is not a ride
GOOD_ENOUGH = 0.55                      # hour score that counts as rideable


def _url(lat: float, lon: float, days: int) -> str:
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        "&hourly=temperature_2m,precipitation_probability,precipitation,"
        "wind_gusts_10m,weather_code,is_day"
        "&daily=sunrise,sunset"
        f"&forecast_days={days}&timezone=auto"
    )


def fetch_hourly(lat: float, lon: float, days: int = 7) -> dict:
    """Live, else the last cache, else nothing. A demo must not need wifi."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"hourly_{lat:.2f}_{lon:.2f}.json")
    try:
        with urllib.request.urlopen(_url(lat, lon, days), timeout=TIMEOUT_S) as r:
            data = json.load(r)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return {**data, "source": "live"}
    except Exception:
        if os.path.exists(path):
            with open(path) as fh:
                return {**json.load(fh), "source": "cache"}
        return {"source": "unavailable"}


def _temp_score(t: float | None) -> float:
    if t is None:
        return 0.5
    if IDEAL_LO <= t <= IDEAL_HI:
        return 1.0
    if t < IDEAL_LO:
        return max(0.0, (t - COLD_FLOOR) / (IDEAL_LO - COLD_FLOOR))
    return max(0.0, (HOT_CEIL - t) / (HOT_CEIL - IDEAL_HI))


def _gust_score(g: float | None) -> float:
    if g is None:
        return 0.7
    if g <= GUST_OK:
        return 1.0
    return max(0.0, (GUST_BAD - g) / (GUST_BAD - GUST_OK))


def hour_score(temp, pop, precip, gust, is_day) -> tuple[float, str]:
    """0..1 plus the single reason it is not 1. Ordered by what ends a ride."""
    if not is_day:
        return 0.0, "dark"
    rain = 1.0 - min(1.0, (pop or 0) / 100.0)
    if (precip or 0) > 0.2:
        rain = min(rain, 0.15)
    temp_s, gust_s = _temp_score(temp), _gust_score(gust)
    # Multiplicative: any one of these being bad is enough on a bike.
    score = rain ** 1.6 * temp_s * gust_s
    worst = min((rain, "rain"), (temp_s, "cold" if (temp or 99) < IDEAL_LO else "heat"),
                (gust_s, "wind"), key=lambda x: x[0])
    return score, ("good" if score >= 0.75 else worst[1])


def windows(lat: float, lon: float, days: int = 7,
            min_hours: int = MIN_WINDOW_H, now: dt.datetime | None = None):
    """Contiguous rideable stretches, best first."""
    data = fetch_hourly(lat, lon, days)
    h = data.get("hourly")
    if not h:
        return {"available": False, "source": data.get("source", "unavailable"),
                "windows": []}

    now = now or dt.datetime.now()
    times = [dt.datetime.fromisoformat(t) for t in h["time"]]
    scored = []
    for i, t in enumerate(times):
        if t < now:
            continue
        s, why = hour_score(
            (h.get("temperature_2m") or [None] * len(times))[i],
            (h.get("precipitation_probability") or [None] * len(times))[i],
            (h.get("precipitation") or [None] * len(times))[i],
            (h.get("wind_gusts_10m") or [None] * len(times))[i],
            (h.get("is_day") or [1] * len(times))[i])
        scored.append((t, s, why, i))

    out, run = [], []
    for row in scored + [(None, 0.0, "", -1)]:
        if row[1] >= GOOD_ENOUGH:
            run.append(row)
            continue
        if len(run) >= min_hours:
            out.append(_window(run, h))
        run = []

    out.sort(key=lambda w: (-w["score"], w["start"]))
    return {"available": True, "source": data.get("source"), "windows": out}


def _window(run, h) -> dict:
    start, end = run[0][0], run[-1][0] + dt.timedelta(hours=1)
    idx = [r[3] for r in run]
    temps = [(h.get("temperature_2m") or [])[i] for i in idx]
    gusts = [(h.get("wind_gusts_10m") or [])[i] for i in idx]
    pops = [(h.get("precipitation_probability") or [])[i] for i in idx]
    hours = len(run)
    mean = sum(r[1] for r in run) / hours
    # A long good window beats a short perfect one: you can ride further.
    return {
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "day": start.strftime("%A"),
        "hours": hours,
        "ride_minutes": int(min(hours * 60, 360)),
        "score": round(mean * min(1.0, 0.55 + 0.15 * hours), 3),
        "quality": round(mean, 3),
        "temp_c": round(sum(t for t in temps if t is not None) / max(len(temps), 1), 1),
        "gust_kmh": round(max([g for g in gusts if g is not None] or [0]), 0),
        "rain_pct": round(max([p for p in pops if p is not None] or [0]), 0),
    }


def headline(w: dict) -> str:
    """The notification text. One sentence, no adjectives it cannot back up."""
    s, e = (dt.datetime.fromisoformat(w[k]) for k in ("start", "end"))
    when = "Today" if s.date() == dt.date.today() else (
        "Tomorrow" if s.date() == dt.date.today() + dt.timedelta(days=1)
        else s.strftime("%A"))
    return (f"{when} {s:%H:%M}–{e:%H:%M} · {w['temp_c']:.0f}°C, "
            f"{w['rain_pct']:.0f}% rain, gusts {w['gust_kmh']:.0f} km/h")
