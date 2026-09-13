"""Small, honest defaults for running the product demo without BMW telemetry."""

from __future__ import annotations

import copy
import datetime as dt


# OSM still provides the real roads. An empty crowd layer makes the road router
# use its documented neutral score and road-class speed fallbacks.
NEUTRAL_CELL_GRAPH = {
    "level": 18,
    "cells": {},
    "edges": [],
    "region_speed_kmh": 50.0,
    "stats": {"source": "lightweight-demo"},
}


SAMPLE_PROFILES = {
    "A": {
        "trips": 42, "km_total": 1180, "squares_ridden": 2840,
        "lean_p95": 16.7, "lean_ceiling": 19.3,
        "speed_mean_kmh": 54.0, "rpm_mean": 3120, "rpm_per_kmh": 49.5,
        "bike_class": "roadster", "top_gear_seen": 6,
        "experience": "intermediate", "experience_score": 0.58,
        "home": {"lat": 48.137, "lon": 11.576},
        "typical_ride_min": 90, "longest_ride_min": 240,
    },
    "B": {
        "trips": 31, "km_total": 1640, "squares_ridden": 3310,
        "lean_p95": 14.8, "lean_ceiling": 17.0,
        "speed_mean_kmh": 59.0, "rpm_mean": 2650, "rpm_per_kmh": 38.4,
        "bike_class": "tourer / adventure", "top_gear_seen": 6,
        "experience": "intermediate", "experience_score": 0.52,
        "home": {"lat": 48.117, "lon": 11.539},
        "typical_ride_min": 120, "longest_ride_min": 310,
    },
    "C": {
        "trips": 67, "km_total": 2960, "squares_ridden": 5180,
        "lean_p95": 21.2, "lean_ceiling": 26.5,
        "speed_mean_kmh": 66.0, "rpm_mean": 4380, "rpm_per_kmh": 66.4,
        "bike_class": "sport", "top_gear_seen": 6,
        "experience": "advanced", "experience_score": 0.81,
        "home": {"lat": 48.151, "lon": 11.558},
        "typical_ride_min": 150, "longest_ride_min": 360,
    },
}


def sample_profile(rider: str) -> dict:
    out = copy.deepcopy(SAMPLE_PROFILES[rider])
    out.update({
        "available": True,
        "ridden_squares": [],
        "demo_mode": True,
        "profile_source": "bundled sample values (not telemetry)",
    })
    return out


def sample_weather(now: dt.datetime | None = None) -> dict:
    """Two future windows so the offline demo always has a Plan button."""
    now = now or dt.datetime.now().astimezone()
    first_day = now.date() if now.hour < 14 else now.date() + dt.timedelta(days=1)
    specs = (
        (first_day, 16, 20, 17.0, 8, 24, 0.91, "full", 4),
        (first_day + dt.timedelta(days=1), 16, 20, 22.0, 45, 34, 0.48, "partial", 2),
        (first_day + dt.timedelta(days=2), 16, 20, 20.0, 12, 20, 0.86, "full", 4),
        (first_day + dt.timedelta(days=3), 16, 20, 31.5, 5, 18, 0.30, "none", 0),
        (first_day + dt.timedelta(days=4), 16, 20, 16.0, 70, 38, 0.22, "none", 0),
    )
    windows = []
    for day, start_h, end_h, temp, rain, gust, score, availability, good_hours in specs:
        start = dt.datetime.combine(day, dt.time(start_h), tzinfo=now.tzinfo)
        end = dt.datetime.combine(day, dt.time(end_h), tzinfo=now.tzinfo)
        best = ({"start": start.isoformat(timespec="minutes"),
                 "end": (start + dt.timedelta(hours=good_hours)).isoformat(timespec="minutes"),
                 "hours": good_hours} if good_hours else None)
        windows.append({
            "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"),
            "day": start.strftime("%A"),
            "hours": end_h - start_h,
            "ride_minutes": min((end_h - start_h) * 60, 360),
            "suggested_minutes": 150,
            "score": score, "quality": score,
            "temp_c": temp, "gust_kmh": gust, "rain_pct": rain,
            "max_temp_c": temp, "confirmed": availability == "full",
            "availability": availability, "good_hours": good_hours,
            "best_period": best,
            "rejection_reason": (None if availability == "full" else
                                 "rain" if availability == "partial" else
                                 "over 30°C" if temp > 30 else "rain"),
            "sunset": dt.datetime.combine(day, dt.time(19),
                                            tzinfo=now.tzinfo).isoformat(timespec="minutes"),
        })
    hour = now.replace(minute=0, second=0, microsecond=0)
    return {"available": True, "source": "bundled sample", "windows": windows,
            "checked_windows": windows,
            "current_hour": {
                "time": hour.isoformat(timespec="minutes"),
                "temp_c": 17.0, "rain_pct": 8, "precip_mm": 0.0,
                "gust_kmh": 24, "weather_code": 2, "is_day": True,
                "ride_score": 0.91, "ride_condition": "good",
            }}
