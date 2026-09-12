"""The four KPI scores, and the cost function.

plan/ALGORITHM.md sections 8 and 9. Everything is normalised per kilometre and
percentile-scaled against the crowd, so the terms are comparable and
dimensionless.
"""

from __future__ import annotations

import bisect

# --- risk weights (cost multiplier terms) --------------------------------
W_ABS = 0.45          # ABS engagements per km: crowd-sourced surface/hazard proxy
W_DECEL = 0.25        # hard deceleration per km
W_CRAWL = 0.30        # standstills -- a red flag in BMW's brief
W_WEATHER = 1.00      # scaled 0..1 by the weather module

# --- scenic mix ----------------------------------------------------------
W_SCENIC_CURVE = 0.40
W_SCENIC_ELEV = 0.25
W_SCENIC_FLOW = 0.20
W_SCENIC_BAND = 0.15

# Confidence: a segment needs roughly this many trips before its crowd score is
# taken at face value. Below that it is shrunk toward the regional mean, so a
# single rider's outlier cannot mint a five-star road.
CONFIDENCE_K = 5.0


class Percentiles:
    """Percentile-scales a feature against the crowd distribution."""

    def __init__(self, segments: list[dict], field: str, derive=None):
        vals = []
        for s in segments:
            v = derive(s) if derive else s.get(field)
            if v is not None:
                vals.append(float(v))
        vals.sort()
        self.vals = vals

    def __call__(self, v: float | None) -> float:
        if v is None or not self.vals:
            return 0.0
        i = bisect.bisect_left(self.vals, float(v))
        return i / len(self.vals)


class Scorer:
    """Scores segments for one rider."""

    def __init__(self, segments: list[dict], profile: dict):
        self.segments = segments
        self.profile = profile
        self.p = {
            "curviness": Percentiles(segments, "curviness"),
            "leaned_share": Percentiles(segments, "leaned_share"),
            "band_share": Percentiles(segments, "band_share"),
            "speed_mean": Percentiles(segments, "speed_mean"),
            "elev_mean": Percentiles(segments, "elev_mean"),
            "abs_rate": Percentiles(segments, "abs_rate"),
        }
        self.weights = profile["taste"]["weights"]
        cap = profile["capability"]
        # The ceiling is road-normalised: the rider's own style ratio applied to
        # what the crowd needs here, times a margin. Never overridable.
        self.style_ratio = cap["style_ratio"]
        self.margin = cap["margin"]
        self.mean_curviness = (
            sum(s["curviness"] for s in segments) / len(segments) if segments else 0.0
        )

    # -- confidence --------------------------------------------------------
    def confidence(self, s: dict) -> float:
        n = float(s.get("n_trips") or 0)
        return n / (n + CONFIDENCE_K)

    # -- the four scores ---------------------------------------------------
    def scenic(self, s: dict) -> float:
        flow = 1.0 - float(s.get("crawl_share") or 0.0)
        return (
            W_SCENIC_CURVE * self.p["curviness"](s.get("curviness"))
            + W_SCENIC_ELEV * self.p["elev_mean"](s.get("elev_mean"))
            + W_SCENIC_FLOW * flow
            + W_SCENIC_BAND * self.p["band_share"](s.get("band_share"))
        )

    def fun(self, s: dict) -> float:
        """Taste-weighted. This is where the rider's own profile bites."""
        c = self.confidence(s)
        # Shrink curviness toward the regional mean when few riders have been
        # here, rather than trusting one trace.
        curv = c * float(s.get("curviness") or 0.0) + (1 - c) * self.mean_curviness
        parts = {
            "curviness": self.p["curviness"](curv),
            "leaned_share": self.p["leaned_share"](s.get("leaned_share")),
            "speed_mean": self.p["speed_mean"](s.get("speed_mean")),
            "band_share": self.p["band_share"](s.get("band_share")),
            "elev_mean": self.p["elev_mean"](s.get("elev_mean")),
        }
        return sum(self.weights.get(k, 0.0) * v for k, v in parts.items())

    def risk(self, s: dict, weather: float = 0.0) -> float:
        return min(
            1.0,
            W_ABS * self.p["abs_rate"](s.get("abs_rate"))
            + W_DECEL * min(1.0, float(s.get("hard_decel_rate") or 0.0) / 5.0)
            + W_CRAWL * float(s.get("crawl_share") or 0.0)
            + W_WEATHER * weather,
        )

    def growth(self, s: dict) -> float:
        """Is this segment a small stretch beyond what the rider has done?

        Reward only the band just above their demonstrated level. Below it is
        familiar, above the ceiling is excluded elsewhere.
        """
        demand = float(s.get("lean_p95") or 0.0)
        current = float(self.profile["capability"]["lean_p90"] or 0.0)
        if current <= 0 or demand <= current:
            return 0.0
        step = max(3.0, 0.10 * current)
        over = demand - current
        return 1.0 if over <= step else max(0.0, 1.0 - (over - step) / step)

    # -- capability gate ---------------------------------------------------
    def excluded(self, s: dict, weather_factor: float = 1.0) -> bool:
        """Hard exclusion. A penalty could be outvoted by a big fun bonus;
        an exclusion cannot. That distinction is the point."""
        demand = float(s.get("lean_p95") or 0.0)
        if demand <= 0:
            return False
        ceiling = demand * 0.0 + (
            float(self.profile["capability"]["lean_p95"] or 0.0)
            * self.margin
            / max(self.style_ratio, 0.3)
        ) * weather_factor
        return demand > ceiling

    # -- cost --------------------------------------------------------------
    def value(self, s: dict, w_growth: float = 0.0) -> float:
        base = 0.55 * self.fun(s) + 0.45 * self.scenic(s)
        if w_growth:
            base = (1 - w_growth) * base + w_growth * self.growth(s)
        return max(0.0, min(1.0, base))

    def seconds(self, s: dict) -> float:
        """Travel time from OBSERVED speed, not a speed limit.

        Fallback chain: segment median -> regional median -> a floor, so a
        missing speed can never produce a divide-by-zero shortcut.
        """
        v = float(s.get("speed_mean") or 0.0)
        if v < 5.0:
            v = 30.0
        return (float(s["length_m"]) / 1000.0) / v * 3600.0

    def cost(self, s: dict, alpha: float, beta: float = 1.0,
             w_growth: float = 0.0, weather: float = 0.0,
             junction_s: float = 0.0) -> float:
        """cost = time * (1 + a*(1-value) + b*risk) + junction_delay

        Every term is positive and the multiplier is >= 1, so Dijkstra stays
        provably correct. Junction delay is ADDITIVE and outside alpha: it is a
        real delay, not a matter of taste, so the style slider must not be able
        to wish it away.
        """
        t = self.seconds(s)
        v = self.value(s, w_growth)
        r = self.risk(s, weather)
        return t * (1.0 + alpha * (1.0 - v) + beta * r) + junction_s

    def kpis(self, s: dict) -> dict:
        return {
            "fun": round(self.fun(s), 3),
            "scenic": round(self.scenic(s), 3),
            "risk": round(self.risk(s), 3),
            "growth": round(self.growth(s), 3),
            "confidence": round(self.confidence(s), 3),
        }
