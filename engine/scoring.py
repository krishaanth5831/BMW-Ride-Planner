"""Scenic score and the cost function.

SCENIC-ONLY MODE. The route value term is the scenic score and nothing else --
no fun weighting, no growth, no rider taste weights. The rider profile is still
used for two things that are not preferences:

  * CAPABILITY, as a hard exclusion. Deliberately kept: a scenic road the rider
    cannot safely ride is not a good recommendation, and a penalty could be
    outvoted by a large enough scenic bonus.
  * time and duration targeting.

Scenic inputs (plan/ALGORITHM.md section 8):
    curviness        lean change per ridden km, blended with road geometry
                     by crowd confidence
    class_scenic     road class -- a motorway is fast, safe and unscenic
    elevation        higher is better
    flow             1 - share of samples crawling (the "standstills" red flag)
    band_share       share of time in the brief's 50-120 km/h green flag
    tunnel           penalty: no view inside a tunnel
"""

from __future__ import annotations

import bisect
import datetime as dt

from engine import sun

# ---------------------------------------------------------------------------
# (*) PROVENANCE MARKER
# A trailing  (*)  marks a number WE CHOSE OURSELVES. It does not come from the
# BMW dataset, from an API, or from the BMW brief -- it is a tuned guess.
# Anything NOT marked (*) is traceable to a source, named in the comment.
# Full inventory: plan/PROVENANCE.md
# ---------------------------------------------------------------------------

# --- scenic mix (sums to 1 before the tunnel penalty) --------------------
# Every weight below is our own judgement about what makes a road scenic.
# Nothing in the dataset or the brief assigns these numbers.
W_CLASS = 0.22          # (*)
W_ELEV = 0.15           # (*)
W_RELIEF = 0.15         # (*)
W_FOREST = 0.10         # (*)
W_WATER_VIEW = 0.10     # (*)
W_CURVE = 0.28          # (*)
W_FLOW = 0.0            # (*)
W_GRADIENT = 0.0        # (*)
W_BAND = 0.0            # (*)
TUNNEL_PENALTY = 0.35   # (*)

# Heatmap routes prefer rider roads over motorways without making highways
# impossible. This is a positive cost multiplier, so Dijkstra remains valid.
HIGHWAY_AVOIDANCE = {          # every value (*)
    "motorway": 5.0, "motorway_link": 4.0,
    "trunk": 3.0, "trunk_link": 2.5,
    "primary": 1.0, "primary_link": 0.8,
}
# (*) Congestion guess per road class. NOT measured. The real version derives
# this from crowd median speed by time band (plan/ALGORITHM.md section 3);
# until that is wired, these are placeholders.
TRAFFIC_CLASS_PRIOR = {        # every value (*)
    "motorway": 0.55, "motorway_link": 0.45,
    "trunk": 0.45, "trunk_link": 0.35,
    "primary": 0.32, "primary_link": 0.25,
    "secondary": 0.18, "secondary_link": 0.14,
    "tertiary": 0.10, "tertiary_link": 0.08,
    "unclassified": 0.05,
}

FUN_WEIGHTS = {                # every value (*)
    "curviness": 0.40,
    "leaned_share": 0.20,
    "band_share": 0.15,
    "flow": 0.10,
    "gradient": 0.10,
    "rpm_sweetspot": 0.05,
}

# --- risk (kept as a cost multiplier; safety is not a preference) --------
# The INPUTS are measured (ABS engagements, decelerations, crawl share all come
# from BMW telemetry). How much each one matters is our call.
W_ABS = 0.45            # (*)
W_DECEL = 0.25          # (*)
W_CRAWL = 0.30          # (*)
W_WEATHER = 1.00        # (*)

# A segment needs roughly this many trips before its crowd-measured curviness is
# trusted outright. Below it, geometry carries more of the weight.
CONFIDENCE_K = 5.0      # (*)


class Percentiles:
    """Percentile-scales a feature against the population of segments."""

    def __init__(self, segments: list[dict], field: str):
        vals = [float(s.get(field) or 0.0) for s in segments
                if s.get(field) is not None]
        vals.sort()
        self.vals = vals

    def __call__(self, v) -> float:
        if not v or not self.vals:
            return 0.0
        return bisect.bisect_left(self.vals, float(v)) / len(self.vals)


class Scorer:
    def __init__(self, segments: list[dict], profile: dict | None = None):
        self.segments = segments
        self.profile = profile
        self.p = {
            "curviness": Percentiles(segments, "curviness"),
            "curvature_geo": Percentiles(segments, "curvature_geo"),
            "elev_mean": Percentiles(segments, "elev_mean"),
            "dem_elev_m": Percentiles(segments, "dem_elev_m"),
            "dem_relief_m": Percentiles(segments, "dem_relief_m"),
            "band_share": Percentiles(segments, "band_share"),
            "leaned_share": Percentiles(segments, "leaned_share"),
            "abs_rate": Percentiles(segments, "abs_rate"),
            "dem_gradient_pct": Percentiles(segments, "dem_gradient_pct"),
            "rpm_mean": Percentiles(segments, "rpm_mean"),
            "speed_mean": Percentiles(segments, "speed_mean"),
            "forest_share": Percentiles(segments, "forest_share"),
            "water_prox": Percentiles(segments, "water_prox"),
        }
        cap = (profile or {}).get("capability") or {}
        self.lean_p95 = float(cap.get("lean_p95") or 0.0)
        self.style_ratio = float(cap.get("style_ratio") or 1.0)
        self.margin = float(cap.get("margin") or 1.15)
        self.experience_level = (profile or {}).get("experience_level")
        self.alpha_max = float((profile or {}).get("alpha_max") or 10.0)
        self.novice_curviness_p70 = sorted(
            float(s.get("curviness") or 0.0) for s in segments
        )[max(0, int(0.70 * max(len(segments) - 1, 0)))] if segments else 0.0
        self.personal_lambda = self._personal_lambda(profile or {})
        self._scenic_cache: dict[int, float] = {}
        self._fun_cache: dict[int, float] = {}
        self._personal_cache: dict[int, float] = {}
        self._risk_cache: dict[tuple[int, float], float] = {}
        self._twist_cache: dict[int, float] = {}
        self._traffic_cache: dict[int, float] = {}

    def _personal_lambda(self, profile: dict) -> float:
        weights = ((profile.get("taste") or {}).get("weights") or {})
        if not weights:
            return 0.5
        visual = sum(float(weights.get(f, 0.0))
                     for f in ("curviness", "elev_mean"))
        dynamic = sum(float(weights.get(f, 0.0))
                      for f in ("leaned_share", "band_share", "speed_mean"))
        total = visual + dynamic
        return visual / total if total else 0.5

    # ------------------------------------------------------------------
    def confidence(self, s: dict) -> float:
        n = float(s.get("n_trips") or 0)
        return n / (n + CONFIDENCE_K)

    # ---------------------------------------------------------------- f(t)
    def set_time(self, when: dt.datetime | None, lat: float, lon: float) -> dict:
        """Bind the scorer to a departure time. Scenic is a function of it.

        Three things genuinely move with the clock:
          * how much light there is at all -- scenery is worth little at night
          * golden hour, and whether a road actually points at the low sun
          * congestion, from the crowd's own speeds in that time band
        """
        if when is None:
            self._sun = None
            return {"available": False}
        ctx = sun.context(when, lat, lon)
        self._sun = ctx
        self._band = ("morning" if when.hour < 11
                      else "midday" if when.hour < 16 else "evening")
        return ctx

    def time_factor(self, s: dict) -> tuple[float, float]:
        """(multiplier, golden bonus) applied to the static scenic score."""
        ctx = getattr(self, "_sun", None)
        if not ctx:
            return 1.0, 0.0
        light = ctx["daylight_factor"]
        gold = ctx["golden_hour"]
        bonus = 0.0
        if gold > 0:
            facing = sun.facing_bonus(s.get("bearing_mean"), ctx["sun_azimuth_deg"])
            # Riding toward a low sun is the money shot; riding away from it is
            # merely pleasant. Capped so it tunes the score, never dominates it.
            bonus = 0.30 * gold * facing
        return light, bonus

    def scenic(self, s: dict) -> float:
        """The only value term in scenic-only mode, evaluated at the set time."""
        sid = s.get("seg_id")
        if sid is not None and sid in self._scenic_cache:
            return self._scenic_cache[sid]
        c = self.confidence(s)
        # Measured lean where riders have been; road geometry where they have
        # not. Both are percentile-scaled so they are on the same footing.
        curve = self.curviness_blended(s)
        flow = 1.0 - float(s.get("crawl_share") or 0.0)
        # Terrain from the DEM where we have it; the trips' own GPS altitude
        # only covers crowd-ridden roads, so it is the fallback, not the source.
        if s.get("dem_elev_m") is not None:
            elev = self.p["dem_elev_m"](s.get("dem_elev_m"))
        else:
            elev = self.p["elev_mean"](s.get("elev_mean"))
        relief = self.p["dem_relief_m"](s.get("dem_relief_m"))
        forest = self.p["forest_share"](s.get("forest_share"))
        water = self.p["water_prox"](s.get("water_prox"))
        viewpoint = float(s.get("viewpoint") or 0.0)
        water_view = min(1.0, 0.7 * water + 0.3 * viewpoint)
        v = (W_CLASS * float(s.get("class_scenic") or 0.5)
             + W_ELEV * elev
             + W_RELIEF * relief
             + W_FOREST * forest
             + W_WATER_VIEW * water_view
             + W_CURVE * curve)
        if s.get("tunnel"):
            v *= (1.0 - TUNNEL_PENALTY)
        # scenic = f(t): scale by available light, then add the golden-hour
        # facing bonus. A road pointing west at 19:30 in September scores
        # higher than the same road at midnight, which is the whole point.
        light, bonus = self.time_factor(s)
        v = max(0.0, min(1.0, v * light + bonus))
        if sid is not None:
            self._scenic_cache[sid] = v
        return v

    def curviness_blended(self, s: dict) -> float:
        """Blend crowd lean rhythm with 15 m-resampled road geometry."""
        n = float(s.get("n_trips") or 0.0)
        c = n / (n + CONFIDENCE_K)
        return (c * self.p["curviness"](s.get("curviness"))
                + (1.0 - c) * self.p["curvature_geo"](s.get("curvature_geo")))

    def twist_score(self, s: dict) -> float:
        """0..1 preference signal for roads with actual repeated direction changes."""
        sid = s.get("seg_id")
        if sid is not None and sid in self._twist_cache:
            return self._twist_cache[sid]
        value = (0.65 * self.curviness_blended(s)
                 + 0.35 * self.p["curvature_geo"](s.get("curvature_geo")))
        value = max(0.0, min(1.0, value))
        if sid is not None:
            self._twist_cache[sid] = value
        return value

    def traffic_pressure(self, s: dict) -> float:
        """0..1 congestion pressure from crowd flow plus a road-class prior."""
        sid = s.get("seg_id")
        if sid is not None and sid in self._traffic_cache:
            return self._traffic_cache[sid]
        n = float(s.get("n_trips") or 0.0)
        confidence = n / (n + CONFIDENCE_K)
        observed = (0.7 * float(s.get("crawl_share") or 0.0)
                    + 0.3 * (1.0 - float(s.get("band_share") or 0.0)))
        prior = TRAFFIC_CLASS_PRIOR.get(s.get("highway"), 0.12)
        value = max(0.0, min(1.0, confidence * observed
                              + (1.0 - confidence) * prior))
        if sid is not None:
            self._traffic_cache[sid] = value
        return value

    def _gradient(self, s: dict) -> float:
        return self.p["dem_gradient_pct"](abs(float(s.get("dem_gradient_pct") or 0.0)))

    def _rpm_sweetspot(self, s: dict) -> float:
        rpm = float(s.get("rpm_mean") or 0.0)
        if not rpm:
            return 0.0
        # A broad motorcycle sweet spot, avoiding a false precision claim when
        # bike-specific gear ratios are unavailable.
        return max(0.0, 1.0 - abs(rpm - 4500.0) / 4500.0)

    def fun(self, s: dict) -> float:
        """Dynamic enjoyment score, percentile-scaled to the network."""
        sid = s.get("seg_id")
        if sid is not None and sid in self._fun_cache:
            return self._fun_cache[sid]
        value = (
            FUN_WEIGHTS["curviness"] * self.curviness_blended(s)
            + FUN_WEIGHTS["leaned_share"] * self.p["leaned_share"](s.get("leaned_share"))
            + FUN_WEIGHTS["band_share"] * self.p["band_share"](s.get("band_share"))
            + FUN_WEIGHTS["flow"] * (1.0 - float(s.get("crawl_share") or 0.0))
            + FUN_WEIGHTS["gradient"] * self._gradient(s)
            + FUN_WEIGHTS["rpm_sweetspot"] * self._rpm_sweetspot(s)
        )
        value = max(0.0, min(1.0, value))
        if sid is not None:
            self._fun_cache[sid] = value
        return value

    def personal(self, s: dict) -> float:
        """Taste-weighted score; capability remains an exclusion elsewhere."""
        sid = s.get("seg_id")
        if sid is not None and sid in self._personal_cache:
            return self._personal_cache[sid]
        value = max(0.0, min(1.0,
            self.personal_lambda * self.scenic(s)
            + (1.0 - self.personal_lambda) * self.fun(s)))
        if sid is not None:
            self._personal_cache[sid] = value
        return value

    def risk(self, s: dict, weather: float = 0.0) -> float:
        sid = s.get("seg_id")
        cache_key = (sid, float(weather)) if sid is not None else None
        if cache_key is not None and cache_key in self._risk_cache:
            return self._risk_cache[cache_key]
        value = min(1.0,
                   W_ABS * self.p["abs_rate"](s.get("abs_rate"))
                   + W_DECEL * min(1.0, float(s.get("hard_decel_rate") or 0.0) / 5.0)
                   + W_CRAWL * float(s.get("crawl_share") or 0.0)
                   + W_WEATHER * weather)
        if cache_key is not None:
            self._risk_cache[cache_key] = value
        return value

    # ------------------------------------------------------------------
    def excluded(self, s: dict, weather_factor: float = 1.0) -> bool:
        """Hard capability gate. Never overridable by a scenic bonus."""
        if self.experience_level == "novice" and float(s.get("curviness") or 0.0) > self.novice_curviness_p70:
            return True
        if not self.lean_p95:
            return False
        demand = float(s.get("lean_p95") or 0.0)
        if demand <= 0:
            return False   # no crowd lean here: nothing to gate on
        ceiling = (self.lean_p95 * self.margin / max(self.style_ratio, 0.3)) * weather_factor
        return demand > ceiling

    def seconds(self, s: dict) -> float:
        """Observed speed first; road-class assumption only as a fallback.

        Never divide by a missing speed -- that would mint a free shortcut.
        """
        v = float(s.get("speed_mean") or 0.0)
        if v < 5.0:
            v = float(s.get("speed_assumed") or 50)
        return (float(s["length_m"]) / 1000.0) / max(v, 5.0) * 3600.0

    def cost(self, s: dict, alpha: float, beta: float = 1.0,
             weather: float = 0.0, junction_s: float = 0.0,
             highway_avoidance: float = 0.0,
             twist_avoidance: float = 0.0,
             traffic_avoidance: float = 0.0) -> float:
        """cost = time * (1 + a*(1 - scenic) + b*risk) + junction_delay

        All terms positive and the multiplier >= 1, so Dijkstra stays valid.
        Junction delay is additive and outside alpha: it is a real delay, not a
        matter of taste.
        """
        highway_cost = HIGHWAY_AVOIDANCE.get(s.get("highway"), 0.0)
        twist = self.twist_score(s)
        run_km = float(s.get("road_run_km") or s.get("length_m", 0.0) / 1000.0)
        # A long uninterrupted straight is worse than a short straight: the
        # penalty only grows when the road lacks twist density, so a long pass
        # with repeated corners is not punished like a motorway.
        long_straight = min(1.0, max(0.0, (run_km - 0.6) / 2.4)) * (1.0 - twist)
        return (self.seconds(s)
                * (1.0 + alpha * (1.0 - self.scenic(s))
                   + beta * self.risk(s, weather)
                   + max(0.0, highway_avoidance) * highway_cost
                   + max(0.0, twist_avoidance) * ((1.0 - twist) + long_straight)
                   + max(0.0, traffic_avoidance) * self.traffic_pressure(s))
                + junction_s)

    def kpis(self, s: dict) -> dict:
        return {
            "seg_id": s["seg_id"],
            "scenic": round(self.scenic(s), 3),
            "fun": round(self.fun(s), 3),
            "personal": round(self.personal(s), 3),
            "risk": round(self.risk(s), 3),
            "confidence": round(self.confidence(s), 3),
        }
