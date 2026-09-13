"""Open-Meteo -- forecast only, no API key, cached to disk with a frozen fallback.

If the network is unavailable (which is the normal state during a demo) this
returns a neutral result rather than failing the request.
"""

from __future__ import annotations

import os

from engine.cache import forecast_json

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "weather")
TIMEOUT_S = 6


def fetch(lat: float, lon: float) -> dict:
    """Current conditions plus a risk scalar and a capability shrink factor."""
    key = f"current_{lat:.4f}_{lon:.4f}.json"
    path = os.path.join(CACHE_DIR, key)

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,precipitation,wind_gusts_10m,weather_code"
    )
    data = forecast_json(url, path, ttl=300, timeout=TIMEOUT_S, required="current")
    source = data["source"]
    if source == "unavailable":
        # No network and no cache: stay neutral instead of inventing weather.
        return {
            "available": False, "source": "unavailable",
            "risk": 0.0, "capability_factor": 1.0,
            "note": "weather unavailable; risk and ceilings left unmodified",
        }

    cur = (data or {}).get("current", {})
    temp = cur.get("temperature_2m")
    precip = cur.get("precipitation") or 0.0
    gust = cur.get("wind_gusts_10m") or 0.0

    # (*) Risk contribution, 0..1. The MEASUREMENTS are real Open-Meteo values
    # (precipitation, temperature, gusts); how much each one raises risk is our
    # judgement. The 8 C threshold is the one exception -- BMW's brief names
    # temperature as a red flag, though not that exact number.
    risk = 0.0
    if precip > 0:
        risk += min(0.5, 0.15 + precip * 0.1)   # (*)
    if temp is not None and temp < 8:
        risk += 0.2                              # (*) magnitude; threshold from the brief
    if gust > 45:
        risk += 0.15                             # (*) both threshold and magnitude
    risk = min(1.0, risk)

    # Weather shrinks the CEILING, which is what makes growth collapse to zero
    # in the wet without needing a separate rule.
    # (*) Every factor here is ours. Nothing measures how much grip a rider
    # actually loses in the wet -- these are conservative guesses.
    factor = 1.0
    if precip > 0:
        factor = 0.7                             # (*)
    if precip > 0 and (temp is not None and temp < 8):
        factor = 0.5                             # (*)
    if (cur.get("weather_code") or 0) in (71, 73, 75, 77, 85, 86):
        factor = 0.0         # (*) snow -> no growth at all

    return {
        "available": True, "source": source,
        "stale": data.get("stale", False), "cache_age_s": data.get("cache_age_s", 0),
        "temp_c": temp, "precip_mm": precip, "wind_gust_kmh": gust,
        "risk": round(risk, 3), "capability_factor": factor,
    }
