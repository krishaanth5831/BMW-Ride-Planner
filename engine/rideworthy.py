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

from engine.net import https_context

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "fixtures", "weather")
TIMEOUT_S = 8

# Degrees C. Below IDEAL_LO hands get cold through summer gloves; above
# IDEAL_HI full textile kit becomes unpleasant in traffic.
IDEAL_LO, IDEAL_HI = 14.0, 27.0
COLD_FLOOR, HOT_CEIL = 4.0, 35.0

GUST_OK, GUST_BAD = 25.0, 55.0          # km/h
MIN_WINDOW_H = 2       # (*) shorter than this is not a ride -- our call
GOOD_ENOUGH = 0.55     # (*) hour score that counts as rideable -- our threshold
MAX_RIDE_TEMP_C = 30.0  # (*) above this, full kit stops being pleasant -- our call


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
        with urllib.request.urlopen(_url(lat, lon, days), timeout=TIMEOUT_S,
                                    context=https_context()) as r:
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
    """Check each day's four-hour window around local sunset."""
    data = fetch_hourly(lat, lon, days)
    h = data.get("hourly")
    if not h:
        return {"available": False, "source": data.get("source", "unavailable"),
                "windows": []}

    now = now or dt.datetime.now()
    times = [dt.datetime.fromisoformat(t) for t in h["time"]]
    latest = [i for i, timestamp in enumerate(times) if timestamp <= now]
    current_hour = _hour_snapshot(h, times, latest[-1]) if latest else None
    daily = data.get("daily") or {}
    sunsets = [dt.datetime.fromisoformat(value)
               for value in daily.get("sunset", [])]
    checked = []
    for sunset in sunsets:
        start = sunset - dt.timedelta(hours=3)
        end = sunset + dt.timedelta(hours=1)
        if end <= now:
            continue
        indices = [i for i, timestamp in enumerate(times) if start <= timestamp < end]
        if indices:
            checked.append(_sunset_window(h, times, indices, start, end))

    out = [window for window in checked if window["confirmed"]]
    out.sort(key=lambda w: (-w["score"], w["start"]))
    return {"available": True, "source": data.get("source"), "windows": out,
            "checked_windows": checked, "current_hour": current_hour,
            "rule": {"hours_before_sunset": 3, "hours_after_sunset": 1,
                     "max_temp_c": MAX_RIDE_TEMP_C,
                     "minimum_hour_score": GOOD_ENOUGH}}


def _sunset_window(hourly: dict, times: list[dt.datetime], indices: list[int],
                   start: dt.datetime, end: dt.datetime) -> dict:
    def values(key, default=None):
        source = hourly.get(key) or []
        return [source[i] if i < len(source) else default for i in indices]

    temps = values("temperature_2m")
    rain = values("precipitation_probability", 0)
    precip = values("precipitation", 0)
    gusts = values("wind_gusts_10m", 0)
    scores = []
    reasons = []
    checks = []
    for index, temp, pop, amount, gust in zip(indices, temps, rain, precip, gusts):
        # The requested window intentionally extends one hour past sunset, so
        # darkness is not a weather rejection inside this particular check.
        score, reason = hour_score(temp, pop, amount, gust, True)
        scores.append(score)
        reasons.append(reason)
        cool = temp is not None and temp <= MAX_RIDE_TEMP_C
        passed = cool and score >= GOOD_ENOUGH
        checks.append({
            "time": times[index].isoformat(timespec="minutes"),
            "temp_c": temp, "score": round(score, 2), "passed": passed,
            "reason": ("over 30°C" if not cool else reason),
        })
    valid_temps = [value for value in temps if value is not None]
    max_temp = max(valid_temps) if valid_temps else None
    cool_enough = max_temp is not None and max_temp <= MAX_RIDE_TEMP_C
    weather_good = bool(scores) and min(scores) >= GOOD_ENOUGH
    confirmed = cool_enough and weather_good
    if not cool_enough:
        rejection = "over 30°C"
    elif not weather_good:
        rejection = reasons[scores.index(min(scores))]
    else:
        rejection = None
    mean_score = sum(scores) / max(len(scores), 1)
    periods = []
    run = []
    for check in checks + [{"passed": False}]:
        if check["passed"]:
            run.append(check)
            continue
        if run:
            period_start = max(start, dt.datetime.fromisoformat(run[0]["time"]))
            period_end = min(end, dt.datetime.fromisoformat(run[-1]["time"])
                             + dt.timedelta(hours=1))
            duration_h = (period_end - period_start).total_seconds() / 3600.0
            periods.append({"start": period_start.isoformat(timespec="minutes"),
                            "end": period_end.isoformat(timespec="minutes"),
                            "hours": round(duration_h, 1)})
        run = []
    good_hours = sum(1 for check in checks if check["passed"])
    availability = ("full" if good_hours == len(checks) else
                    "partial" if good_hours else "none")
    best_period = max(periods, key=lambda period: period["hours"], default=None)
    return {
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "sunset": (start + dt.timedelta(hours=3)).isoformat(timespec="minutes"),
        "day": start.strftime("%A"), "hours": 4, "ride_minutes": 240,
        "score": round(mean_score, 3), "quality": round(mean_score, 3),
        "temp_c": round(sum(valid_temps) / max(len(valid_temps), 1), 1),
        "max_temp_c": round(max_temp, 1) if max_temp is not None else None,
        "gust_kmh": round(max(gusts or [0]), 0),
        "rain_pct": round(max(rain or [0]), 0),
        "confirmed": confirmed, "rejection_reason": rejection,
        "availability": availability, "good_hours": good_hours,
        "hour_checks": checks, "good_periods": periods,
        "best_period": best_period,
    }


def _hour_snapshot(hourly: dict, times: list[dt.datetime], index: int) -> dict:
    def value(key, default=None):
        values = hourly.get(key) or []
        return values[index] if index < len(values) else default

    temp = value("temperature_2m")
    rain = value("precipitation_probability", 0)
    precip = value("precipitation", 0)
    gust = value("wind_gusts_10m", 0)
    is_day = value("is_day", 1)
    score, reason = hour_score(temp, rain, precip, gust, is_day)
    return {
        "time": times[index].isoformat(timespec="minutes"),
        "temp_c": temp, "rain_pct": rain, "precip_mm": precip,
        "gust_kmh": gust, "weather_code": value("weather_code"),
        "is_day": bool(is_day), "ride_score": round(score, 2),
        "ride_condition": reason,
    }


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
