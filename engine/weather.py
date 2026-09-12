"""Open-Meteo -- forecast only, no API key, cached to disk with a frozen fallback.

If the network is unavailable (which is the normal state during a demo) this
returns a neutral result rather than failing the request.
"""

from __future__ import annotations

import json
import os
import urllib.request

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "weather")
TIMEOUT_S = 6


def fetch(lat: float, lon: float) -> dict:
    """Current conditions plus a risk scalar and a capability shrink factor."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = f"{lat:.2f}_{lon:.2f}.json"
    path = os.path.join(CACHE_DIR, key)

    data = None
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            "&current=temperature_2m,precipitation,wind_gusts_10m,weather_code"
        )
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            data = json.load(r)
        with open(path, "w") as fh:
            json.dump(data, fh)
        source = "live"
    except Exception:
        if os.path.exists(path):
            with open(path) as fh:
                data = json.load(fh)
            source = "cache"
        else:
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

    # Risk contribution, 0..1.
    risk = 0.0
    if precip > 0:
        risk += min(0.5, 0.15 + precip * 0.1)
    if temp is not None and temp < 8:
        risk += 0.2          # a red flag named in BMW's brief
    if gust > 45:
        risk += 0.15
    risk = min(1.0, risk)

    # Weather shrinks the CEILING, which is what makes growth collapse to zero
    # in the wet without needing a separate rule.
    factor = 1.0
    if precip > 0:
        factor = 0.7
    if precip > 0 and (temp is not None and temp < 8):
        factor = 0.5
    if (cur.get("weather_code") or 0) in (71, 73, 75, 77, 85, 86):
        factor = 0.0         # snow

    return {
        "available": True, "source": source,
        "temp_c": temp, "precip_mm": precip, "wind_gust_kmh": gust,
        "risk": round(risk, 3), "capability_factor": factor,
    }
