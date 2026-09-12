"""Sun position from date/time and location -- no API, no dependency.

NOAA solar position approximation. Accurate to well under a degree, which is
far more than enough to decide whether the sun is up, low, or behind you.

This is what makes the scenic score a function of time: a road that is glorious
at 19:30 in June is just a dark road at 23:00, and the score should say so.
"""

from __future__ import annotations

import datetime as dt
import math


def _julian_day(when: dt.datetime) -> float:
    when = when.astimezone(dt.timezone.utc)
    y, m = when.year, when.month
    d = (when.day + when.hour / 24 + when.minute / 1440 + when.second / 86400)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def sun_position(when: dt.datetime, lat: float, lon: float) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg). Azimuth is clockwise from north."""
    jd = _julian_day(when)
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360          # mean longitude
    g = math.radians((357.528 + 0.9856003 * n) % 360)   # mean anomaly
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)

    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))

    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    lst = math.radians((gmst * 15 + lon) % 360)
    ha = lst - ra

    latr = math.radians(lat)
    elev = math.asin(math.sin(latr) * math.sin(dec)
                     + math.cos(latr) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha),
                    math.tan(dec) * math.cos(latr) - math.sin(latr) * math.cos(ha))
    return math.degrees(elev), (math.degrees(az) + 360) % 360


# Light bands. Below the horizon there is progressively less to look at, but the
# road is still rideable -- so scenery decays, it does not vanish.
def daylight_factor(elev_deg: float) -> float:
    if elev_deg >= 5:
        return 1.0
    if elev_deg >= 0:
        return 0.95
    if elev_deg >= -6:      # civil twilight
        return 0.55
    if elev_deg >= -12:     # nautical twilight
        return 0.25
    return 0.12             # night


def golden_hour_strength(elev_deg: float) -> float:
    """1.0 with the sun low and warm, 0 once it is high or gone."""
    if elev_deg <= -4 or elev_deg >= 12:
        return 0.0
    if elev_deg < 0:
        return max(0.0, (elev_deg + 4) / 4) * 0.6
    return max(0.0, 1.0 - elev_deg / 12.0)


def facing_bonus(segment_bearing: float | None, sun_azimuth: float) -> float:
    """How much a road points at the sun. 1 = straight at it, 0 = away."""
    if segment_bearing is None:
        return 0.0
    diff = abs(((segment_bearing - sun_azimuth + 180) % 360) - 180)
    if diff >= 90:
        return 0.0
    return math.cos(math.radians(diff))


def context(when: dt.datetime, lat: float, lon: float) -> dict:
    elev, az = sun_position(when, lat, lon)
    return {
        "when": when.isoformat(),
        "sun_elevation_deg": round(elev, 1),
        "sun_azimuth_deg": round(az, 1),
        "daylight_factor": round(daylight_factor(elev), 3),
        "golden_hour": round(golden_hour_strength(elev), 3),
        "phase": ("day" if elev >= 5 else "golden hour" if elev >= -4
                  else "twilight" if elev >= -12 else "night"),
    }
