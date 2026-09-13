"""The fog map: which roads this rider has actually ridden, and what is left.

plan/UI.md section 5 is specific about the unit, and it is right to be:

    "Fog-of-war over the road network itself [...] Naming road-kilometres
     rather than a percentage of grid squares is both more meaningful and
     more motivating."

So this counts ROAD, not area. "You have ridden 214 of the 3,180 km of good
motorcycling road down here" is a sentence a rider feels. "You have visited
11% of the grid" is not.

A segment counts as ridden when the morton square under its midpoint is one of
the squares this rider's own trips passed through. That is the same square that
carries its crowd score, so exploration and quality are measured on the same
object and cannot drift apart.

The second half of the spec matters as much as the first: unexplored but
FUN-DENSE roads glow as targets. Fog that only shows absence is a guilt trip;
fog that points at the best road you have never ridden is a reason to go out.
"""

from __future__ import annotations

import math
from collections import defaultdict

from engine.cell_osm_router import RURAL_MAX, RoadCost, RoadGraph
from precompute.morton import LEVEL_NODE, morton_code

# (*) A target has to be worth the trip. Tuned by looking at what comes out:
# at 1.2 km the list is 1-2 km fragments that happen to score well, and at 5 km
# it is long dull through-roads. At 3 km it returns Kesselbergstrasse,
# Tegernseer Strasse and Mittenwalder Strasse, which are roads a rider would
# actually name.
MIN_TARGET_M = 3_000.0

# (*) Only roads out of town can be targets. An unridden residential street is
# unridden for a reason.
TARGET_URBAN_MAX = RURAL_MAX

# (*) Which slice of the unridden roads is worth marking as "go here". Taken as
# a PERCENTILE of this rider's own unridden set rather than a fixed score, so
# the red layer stays a shortlist whether somebody has ridden 10% of the region
# or 90% of it. A fixed cutoff would show everything to a new rider and nothing
# to an experienced one.
SCENIC_PERCENTILE = 0.80


def ridden_segments(g: RoadGraph, squares) -> set[int]:
    """Seg ids whose square this rider has been through."""
    squares = set(squares or ())
    if not squares:
        return set()
    out = set()
    for s in g.segments:
        if morton_code(*s["mid"], LEVEL_NODE) in squares:
            out.add(s["seg_id"])
    return out


def _km(g: RoadGraph, seg_ids) -> float:
    return sum(float(g.by_id[i]["length_m"]) for i in seg_ids) / 1000.0


def coverage(g: RoadGraph, ridden: set[int]) -> dict:
    total = sum(float(s["length_m"]) for s in g.segments) / 1000.0
    done = _km(g, ridden)
    return {
        "ridden_km": round(done, 1),
        "network_km": round(total, 1),
        "pct": round(100.0 * done / max(total, 1e-6), 1),
    }


def targets(g: RoadGraph, cost: RoadCost, ridden: set[int], n: int = 8) -> list[dict]:
    """The best roads this rider has never ridden, grouped by road name.

    Grouped, because a single unridden 100 m chunk is not a target -- "the
    Kesselberg, 4.2 km of it unridden" is. Segments with no name fall back to
    their OSM way, so an unnamed pass still surfaces as one road rather than
    forty fragments.
    """
    groups: dict = defaultdict(lambda: {"m": 0.0, "score": 0.0, "pts": [],
                                        "name": None, "highway": None})
    for s in g.segments:
        if s["seg_id"] in ridden:
            continue
        if g.urban_at(*s["mid"]) > TARGET_URBAN_MAX:
            continue
        cell = g.cell_of(s)
        if cell is None:
            continue          # never ridden by anyone: no evidence it is good
        key = s.get("name") or ("way", s.get("way_id"))
        grp = groups[key]
        L = float(s["length_m"])
        grp["m"] += L
        grp["score"] += cost.value(s) * L
        grp["name"] = s.get("name")
        grp["highway"] = s.get("highway")
        grp["pts"].append(s["mid"])

    out = []
    for key, grp in groups.items():
        if grp["m"] < MIN_TARGET_M:
            continue
        lat = sum(p[0] for p in grp["pts"]) / len(grp["pts"])
        lon = sum(p[1] for p in grp["pts"]) / len(grp["pts"])
        out.append({
            "name": grp["name"] or "Unnamed road",
            "highway": grp["highway"],
            "km": round(grp["m"] / 1000.0, 1),
            "value": round(grp["score"] / grp["m"], 3),
            "lat": round(lat, 5), "lon": round(lon, 5),
        })
    # Quality first, then length: a long mediocre road is not a better target
    # than a short brilliant one, but between equals, longer wins.
    out.sort(key=lambda t: (-t["value"], -t["km"]))
    # "Ride the Kesselbergstrasse" is a plan. "Ride an unnamed road" is not, so
    # named roads go first and unnamed ones only fill the list if it is short.
    named = [t for t in out if t["name"] != "Unnamed road"]
    return (named + [t for t in out if t["name"] == "Unnamed road"])[:n]


def lines(g: RoadGraph, seg_ids, simplify: bool = True) -> list:
    """Compact wire format: one [lat1, lon1, lat2, lon2] per segment.

    Segments are ~100 m chunks, so their end-to-end chord is visually identical
    to the full polyline at any zoom the fog map is read at, and it is roughly
    a tenth of the bytes.
    """
    out = []
    for i in seg_ids:
        geom = g.by_id[i]["geometry"]
        if simplify:
            a, b = geom[0], geom[-1]
            out.append([round(a[0], 5), round(a[1], 5),
                        round(b[0], 5), round(b[1], 5)])
        else:
            out.append([[p[0], p[1]] for p in geom])
    return out


def scenic_unridden(g: RoadGraph, cost: RoadCost, ridden: set[int]):
    """The good roads still in the fog. Red on the map.

    Scored the same way the router scores them, so what glows red is exactly
    what the planner would route you down if you asked. Restricted to roads
    out of town that the crowd has actually ridden: an empty road with no
    evidence behind it is not a recommendation.
    """
    scored = []
    for s in g.segments:
        if s["seg_id"] in ridden:
            continue
        if g.urban_at(*s["mid"]) > TARGET_URBAN_MAX or g.cell_of(s) is None:
            continue
        scored.append((cost.value(s), s["seg_id"]))
    if not scored:
        return [], 0.0
    scored.sort()
    cut = scored[int(SCENIC_PERCENTILE * (len(scored) - 1))][0]
    return [i for v, i in scored if v >= cut], cut


def build(g: RoadGraph, cost: RoadCost, squares, n_targets: int = 8) -> dict:
    ridden = ridden_segments(g, squares)
    cov = coverage(g, ridden)
    scenic, cut = scenic_unridden(g, cost, ridden)
    return {
        **cov,
        "ridden": lines(g, sorted(ridden)),
        "scenic_unridden": lines(g, scenic),
        "scenic_cutoff": round(cut, 3),
        "scenic_unridden_km": round(_km(g, scenic), 1),
        "targets": targets(g, cost, ridden, n_targets),
    }
