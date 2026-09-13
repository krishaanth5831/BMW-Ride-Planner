"""Chain scenic points into a ride, instead of choosing a random curvy way.

The whiteboard's last line is a criticism of turnaround-by-bearing routing, and
it is a fair one. Picking the node furthest away in some compass sector gives
you a curvy road to nowhere: curvature is a property of tarmac, not a reason to
go anywhere. A rider does not set off to ride a radius -- they set off to ride
TO Walchensee, over the Kesselberg, and back along the Kochelsee.

So the ride is built as a chain:

    origin -> Kochelsee -> Kesselberg -> Walchensee -> origin

Each leg is the same distorted Dijkstra, goal-directed, with the roads already
used made more expensive so the way home is not the way out. There is no new
search here and no new cost function -- only a better answer to "where should
this go?".

Ordering is greedy and monotone in distance-from-origin, so the chain runs out
and comes back rather than doubling over itself, which is what makes it read as
a route somebody planned.
"""

from __future__ import annotations

import json
import math
import os
import random

from engine.spatial import PointIndex

from engine.cell_osm_router import (MARIENPLATZ, RURAL_MAX, RoadCost, RoadGraph,
                                    _thirds, bearing, build, dijkstra,
                                    path_segments)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENIC_DIR = os.path.join(ROOT, "fixtures", "scenic")

# South of Munich is where the lakes and the Alps are. When the ride starts in
# a city we aim the whole chain into that half of the compass rather than
# letting it wander north into the flatland -- a candidate FILTER, so the cost
# function and its four properties are untouched.
# Kept deliberately narrow. A 100-260 sector still admits the ponds on the
# eastern edge of Munich, which is how a "scenic ride" came back as 88% inside
# the city. The lakes and the Alps are genuinely south to south-west.
# (*) What counts as a "south-facing" view. Our own sector, not a standard.
SOUTH_SECTOR = (140.0, 235.0)   # (*)

# A few kilometres of shared access road can be unavoidable near home. Beyond
# that, a joy ride must be a loop rather than a scenic destination on a stick.
MAX_RETRACE_M = 8_000.0
MAX_RETRACE_SHARE = 0.10
SCENIC_REUSE_MULTIPLIER = 30.0

# A POI is "visited" if the route passes this close. Riding past the far shore
# of a lake still counts as riding to the lake.
# (*) How near a feature has to be to count as "you can see it from the road".
VISIT_M = 700.0                 # (*)
# (*) How far a viewpoint may sit from a lake and still be a view OF that lake.
LAKE_VIEWPOINT_MAX_M = 8_000.0  # (*)

# (*) Names we chose for the three example riders. The dataset carries no rider
# names, ages or bike models, so these are labels for a demo audience rather
# than anything the telemetry says.
RIDER_TYPES = {
    "A": "The Tourer",
    "B": "The Committed Corner Rider",
    "C": "The Aggressive All-Rounder",
}


def load_pois(path: str | None = None) -> list[dict]:
    if path is None:
        files = [f for f in sorted(os.listdir(SCENIC_DIR)) if f.startswith("poi_")]
        if not files:
            return []
        path = os.path.join(SCENIC_DIR, files[0])
    with open(path) as fh:
        return json.load(fh)["pois"]


def haversine_m(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def in_sector(b: float, sector) -> bool:
    lo, hi = sector
    return lo <= b <= hi if lo <= hi else (b >= lo or b <= hi)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _poi_rider_affinity(graph: RoadGraph, poi: dict) -> dict[str, float]:
    """Deterministic 1–10 POI suitability for the three sample archetypes.

    This uses only attributes available in the bundled fixture and road graph;
    it is a transparent demo heuristic, not learned rider preference.
    """
    segments = [graph.by_id[sid] for sid in graph.adj.get(poi["node"], ())]
    curves = [min(float(s.get("curvature_geo") or 0.0), 300.0)
              for s in segments]
    curvature = _clamp01((sum(curves) / max(len(curves), 1)) / 180.0)
    non_highway = (sum(s.get("highway") not in
                       ("motorway", "motorway_link", "trunk", "trunk_link")
                       for s in segments) / max(len(segments), 1))
    rural = 1.0 - graph.urban_of_node(poi["node"])
    distance = _clamp01(haversine_m(MARIENPLATZ,
                                    graph.node_pos(poi["node"])) / 55_000.0)
    try:
        elevation = _clamp01(float(poi.get("ele") or 0.0) / 1_600.0)
    except (TypeError, ValueError):
        elevation = 0.25
    scenery = _clamp01((float(poi.get("weight") or 1.0) - 1.0) / 0.8)
    access = 1.0 - _clamp01(float(poi.get("snap_distance_m") or 0.0) / 2_500.0)
    kind = poi.get("kind")
    # "unridden" is a road this rider has never been down, injected from the
    # fog map. It scores high for everyone on purpose: somewhere new is the
    # one thing a rider cannot get from a route they have already done, and
    # without this it would take the 0.6 default and never be chosen.
    kind_fit = {
        "A": {"lake": 1.0, "viewpoint": .82, "pass": .62, "peak": .68,
              "unridden": .92},
        "B": {"lake": .52, "viewpoint": .78, "pass": 1.0, "peak": .86,
              "unridden": .90},
        "C": {"lake": .75, "viewpoint": .86, "pass": .96, "peak": .92,
              "unridden": .94},
    }
    road_classes = {s.get("highway") for s in segments}
    tourer_road = (1.0 if road_classes & {"secondary", "tertiary"} else
                   .72 if "primary" in road_classes else .62)

    raw = {
        "A": (.28 * kind_fit["A"].get(kind, .6) + .15 * scenery
              + .15 * rural + .16 * distance + .18 * tourer_road
              + .08 * access),
        "B": (.20 * kind_fit["B"].get(kind, .6) + .32 * curvature
              + .15 * elevation + .12 * rural + .13 * non_highway
              + .08 * access),
        "C": (.20 * kind_fit["C"].get(kind, .7) + .20 * curvature
              + .13 * elevation + .13 * scenery + .10 * distance
              + .08 * rural + .10 * non_highway + .06 * access),
    }
    return {rider: round(max(1.0, min(10.0, 1 + 9 * score)), 3)
            for rider, score in raw.items()}


def _objective_scenic_score(graph: RoadGraph, poi: dict) -> float:
    """A rider-independent 1–10 scenic score from bundled OSM attributes."""
    kind = {"lake": 1.0, "pass": .96, "peak": .90, "viewpoint": .84,
            "unridden": .88}.get(poi.get("kind"), .6)
    weight = _clamp01((float(poi.get("weight") or 1.0) - 1.0) / .8)
    rural = 1.0 - graph.urban_of_node(poi["node"])
    score = 1.0 + 9.0 * (.58 * kind + .27 * weight + .15 * rural)
    return round(max(1.0, min(10.0, score)), 3)


class ScenicIndex:
    """POIs snapped onto the road graph, once."""

    def __init__(self, graph: RoadGraph, pois: list[dict]):
        self.g = graph
        self.pois = []
        viewpoints = [p for p in pois if p.get("kind") == "viewpoint"]
        for p in pois:
            target = p
            target_source = "lake road access" if p.get("kind") == "lake" else "poi"
            if p.get("kind") == "lake" and viewpoints:
                nearby = sorted(
                    viewpoints,
                    key=lambda v: haversine_m((p["lat"], p["lon"]),
                                              (v["lat"], v["lon"])),
                )
                for candidate in nearby:
                    if haversine_m((p["lat"], p["lon"]),
                                   (candidate["lat"], candidate["lon"])) > LAKE_VIEWPOINT_MAX_M:
                        break
                    candidate_node = graph.nearest_node(candidate["lat"],
                                                        candidate["lon"])
                    if candidate_node is not None:
                        candidate_pos = graph.node_pos(candidate_node)
                        if haversine_m((candidate["lat"], candidate["lon"]),
                                       candidate_pos) <= VISIT_M:
                            target = candidate
                            target_source = "nearby viewpoint"
                            break

            node = graph.nearest_node(target["lat"], target["lon"])
            if node is None:
                continue
            pos = graph.node_pos(node)
            # A POI whose nearest road is kilometres away is a summit you would
            # have to walk to. Keep it only if a road actually goes near it.
            if haversine_m((target["lat"], target["lon"]), pos) > 2_500:
                continue
            # The source coordinate describes the attraction, not necessarily
            # a place where a motorcycle can stand. Every public POI and route
            # stop is therefore displayed at the exact OSM road node used by
            # the router. This keeps lake centroids, hilltops and viewpoints
            # from floating beside the road while preserving their source
            # coordinate for provenance.
            display_lat, display_lon = pos
            snap_distance_m = round(haversine_m(
                (target["lat"], target["lon"]), pos))
            item = {**p, "node": node, "road_lat": pos[0],
                    "road_lon": pos[1],
                    "target_name": target["name"],
                    "target_kind": target["kind"],
                    "target_lat": display_lat,
                    "target_lon": display_lon,
                    "source_lat": target["lat"],
                    "source_lon": target["lon"],
                    "snap_distance_m": snap_distance_m,
                    "target_source": target_source + " · OSM road"}
            item["rider_affinity"] = _poi_rider_affinity(graph, item)
            item["objective_scenic"] = _objective_scenic_score(graph, item)
            item["cool_score"] = {
                rider: round(.65 * affinity + .35 * item["objective_scenic"], 3)
                for rider, affinity in item["rider_affinity"].items()
            }
            item["rider_fit"] = {
                rider: round(score)
                for rider, score in item["cool_score"].items()
            }
            local_segments = [graph.by_id[sid]
                              for sid in graph.adj.get(node, ())]
            length_m = sum(float(seg["length_m"]) for seg in local_segments)
            seconds = sum(graph.seconds(seg) for seg in local_segments)
            item["style_features"] = {
                "curvature_deg_per_km": round(
                    sum(float(seg.get("curvature_geo") or 0.0)
                        for seg in local_segments) / max(len(local_segments), 1), 3),
                "road_speed_kmh": round(length_m / max(seconds, .001) * 3.6, 2),
            }
            self.pois.append(item)

        total = len(self.pois)
        # Exclusive style ownership uses the top 30% of each relevant road
        # feature. B owns clearly curvature-dominant POIs, C owns clearly
        # speed-dominant POIs, and ambiguous/non-dominant places default to A.
        top_n = max(1, math.ceil(total * .30))
        by_curve = sorted(
            self.pois,
            key=lambda item: (-item["style_features"]["curvature_deg_per_km"],
                              item["name"], item["node"]))
        by_speed = sorted(
            self.pois,
            key=lambda item: (-item["style_features"]["road_speed_kmh"],
                              item["name"], item["node"]))
        curve_rank = {id(item): rank for rank, item in enumerate(by_curve, 1)}
        speed_rank = {id(item): rank for rank, item in enumerate(by_speed, 1)}
        clear_margin = max(1, math.ceil(total * .05))
        for item in self.pois:
            cr, sr = curve_rank[id(item)], speed_rank[id(item)]
            curve_top, speed_top = cr <= top_n, sr <= top_n
            if curve_top and (not speed_top or cr + clear_margin < sr):
                owner = "B"
            elif speed_top and (not curve_top or sr + clear_margin < cr):
                owner = "C"
            else:
                owner = "A"
            item["style_features"].update({
                "curvature_rank": cr,
                "speed_rank": sr,
                "ranked_out_of": total,
                "top_30_percent_cutoff": top_n,
            })
            item["rider_style_owner"] = owner

        for rider in RIDER_TYPES:
            ordered = sorted(self.pois,
                             key=lambda item: (-item["cool_score"][rider],
                                               item["name"], item["node"]))
            for rank, item in enumerate(ordered, 1):
                item.setdefault("rider_rank", {})[rider] = {
                    "rating": item["rider_fit"][rider],
                    "affinity": item["rider_affinity"][rider],
                    "cool_score": item["cool_score"][rider],
                    "rank": rank,
                    "out_of": total,
                    "rider_type": RIDER_TYPES[rider],
                }

    def visited_by(self, coords: list[list[float]], within_m: float = VISIT_M):
        """Which POIs the finished route actually passes. The honest check."""
        if not coords:
            return []
        # Thin the polyline before the O(n*m) sweep; 2287 points against 600
        # POIs is slow and pointless at 100 m resolution.
        pts = coords[::3] or coords
        point_index = PointIndex((i, c[0], c[1]) for i, c in enumerate(pts))
        # Conservative spherical bounds, then the original exact haversine
        # test. No geometry samples, POIs or route candidates are removed.
        angular = max(0.0, within_m) / 6_371_000.0
        latitude_delta = math.degrees(angular)
        out = []
        for p in self.pois:
            lat, lon = p["target_lat"], p["target_lon"]
            cosine = math.cos(math.radians(lat))
            longitude_delta = (180.0 if abs(lat) + latitude_delta >= 90
                               else math.degrees(math.asin(min(1.0, math.sin(angular)
                                                                 / max(cosine, 1e-15)))))
            # Road fixtures are regional. At the date line use the full sweep.
            nearby = (range(len(pts)) if abs(lon) + longitude_delta >= 180 else
                      point_index.in_box(lat - latitude_delta - 1e-10,
                                         lon - longitude_delta - 1e-10,
                                         lat + latitude_delta + 1e-10,
                                         lon + longitude_delta + 1e-10))
            if not nearby:
                continue
            best = min(haversine_m((p["target_lat"], p["target_lon"]),
                                   (pts[i][0], pts[i][1])) for i in nearby)
            if best <= within_m:
                out.append({"name": p["name"], "kind": p["kind"],
                            "lat": p["target_lat"], "lon": p["target_lon"],
                            "weight": p["weight"],
                            "target_name": p["target_name"],
                            "target_kind": p["target_kind"],
                            "target_lat": p["target_lat"],
                            "target_lon": p["target_lon"],
                            "target_source": p["target_source"],
                            "objective_scenic": p["objective_scenic"],
                            "rider_style_owner": p["rider_style_owner"],
                            "style_features": p["style_features"],
                            "rider_fit": p["rider_fit"],
                            "cool_score": p["cool_score"],
                            "rider_rank": p["rider_rank"],
                            "distance_m": round(best)})
        out.sort(key=lambda q: -q["weight"])
        return out


def _leg(g: RoadGraph, cost: RoadCost, a: int, b: int, alpha: float, used: set):
    # Shortening a rejected chain often repeats an identical first leg.
    # Include the complete reuse set: that penalty must never be lost.
    cache = cost.__dict__.setdefault("_leg_cache", {})
    key = (a, b, alpha, frozenset(used))
    if key in cache:
        return cache[key]
    _d, _s, ps, pn = dijkstra(
        g, cost, a, alpha, goal=b, reuse=used,
        reuse_multiplier=SCENIC_REUSE_MULTIPLIER)
    result = path_segments(ps, pn, a, b)
    if len(cache) < 256:
        cache[key] = result
    return result


def plan_scenic_loop(g: RoadGraph, cost: RoadCost, index: ScenicIndex,
                     origin: int, minutes: float, alpha: float = 3.0,
                     max_stops: int = 3, south_bias: bool | None = None,
                     rider_id: str = "A", rider_profile: dict | None = None,
                     seed: int | str | None = None,
                     must_include: tuple | None = None,
                     retrace: tuple | None = None) -> dict:
    """A varied ride out to real places and back, inside the time budget.

    Randomness only reorders a quality-bounded shortlist and slightly changes
    scenic road pressure. Supplying ``seed`` reproduces the same variation.
    """
    if rider_id not in RIDER_TYPES:
        raise ValueError("unknown rider preference; use A, B, or C")
    variation_seed = (random.SystemRandom().randrange(2 ** 32)
                      if seed is None else seed)
    rng = random.Random(variation_seed)
    varied_alpha = max(0.1, alpha * rng.uniform(0.88, 1.12))
    budget = minutes * 60.0
    origin_pos = g.node_pos(origin)
    profile_ratings = (rider_profile or {}).get("style_ratings", {})
    endurance = float(profile_ratings.get("Long-distance endurance", 5)) / 10.0
    technical = float(profile_ratings.get("Technical aggression", 5)) / 10.0
    exploration = float(profile_ratings.get("Geographic exploration", 5)) / 10.0
    # The Tourer is comfortable spending more of the window on the outbound
    # leg. The committed corner rider prefers a nearer technical destination;
    # the all-rounder sits between those poles. This comes from the profile,
    # not from a hard-coded destination list.
    ideal_reach = _clamp01(.52 + .36 * endurance + .08 * exploration
                           - .14 * technical)

    if south_bias is None:
        # Only aim a city rider south. Somebody already in the foothills has
        # good roads in every direction and should not be herded.
        south_bias = g.urban_of_node(origin) > RURAL_MAX
    sector = SOUTH_SECTOR if south_bias else None

    # One flood gives travel time to every POI at once.
    _d, secs, ps, pn = dijkstra(g, cost, origin, varied_alpha,
                                max_seconds=budget * 0.62)

    cands = []
    for p in index.pois:
        if p.get("rider_style_owner", "A") != rider_id:
            continue
        t = secs.get(p["node"])
        if t is None:
            continue
        b = bearing(origin_pos, (p["road_lat"], p["road_lon"]))
        if sector and not in_sector(b, sector):
            continue
        # A lake with a ring road round a housing estate is not a destination.
        # The stop itself has to be out of town, or the ride never leaves one.
        if g.urban_at(p["road_lat"], p["road_lon"]) > RURAL_MAX:
            continue
        cands.append({**p, "t": t, "bearing": b,
                      "preference": p["cool_score"][rider_id] / 10.0,
                      "dist": haversine_m(origin_pos, (p["road_lat"], p["road_lon"]))})
    if not cands:
        return {"routes": [], "reason":
                "Nothing worth riding to is reachable in this time"
                + (", heading south out of the city." if sector else ".")
                + " Give it longer and the lakes come into range."}

    # Rider/POI affinity is the dominant destination signal. Reach preference
    # adapts distance to the profile, while OSM scenic weight remains an
    # independent quality signal rather than being replaced by personalization.
    for c in cands:
        reach = c["t"] / max(budget * 0.5, 1.0)
        reach_match = max(0.0, 1.0 - abs(reach - ideal_reach))
        scenic = _clamp01((float(c.get("weight") or 1.0) - 1.0) / .8)
        neighbours = [
            (haversine_m((c["target_lat"], c["target_lon"]),
                         (poi["target_lat"], poi["target_lon"])), poi)
            for poi in index.pois
            if poi.get("rider_style_owner", "A") == rider_id
        ]
        c["spot_density"] = sum(
            .25 + .75 * (1.0 - distance_m / 12_000.0)
            for distance_m, _poi in neighbours if distance_m <= 12_000.0
        )
        density = _clamp01(c["spot_density"] / 8.0)
        c["anchor_score"] = (.48 * c["preference"]
                             + .16 * reach_match + .11 * scenic
                             + .25 * density)
    cands.sort(key=lambda c: -c["anchor_score"])

    # Keep every variation among the best-scoring destinations. The small
    # jitter stops one technically optimal anchor from winning every joy ride,
    # without allowing a mediocre POI to leapfrog the quality shortlist.
    shortlist_size = min(8, len(cands))
    shortlist = cands[:shortlist_size]
    randomized_shortlist = sorted(
        shortlist,
        key=lambda c: -(c["anchor_score"] + rng.uniform(-0.09, 0.09)),
    )
    anchors = randomized_shortlist + cands[shortlist_size:18]

    # The fog map asks for a ride to ONE specific road the rider has never
    # been down. Pinning it as the anchor rather than routing there and back
    # means the normal chain builder still adds a companion stop and brings
    # them home a different way -- which is also what keeps it clear of the
    # destination-on-a-stick guard in _ride.
    if must_include:
        m_lat, m_lon = float(must_include[0]), float(must_include[1])
        m_name = must_include[2] if len(must_include) > 2 else "your new road"
        m_node = g.nearest_node(m_lat, m_lon)
        if m_node is not None:
            pos = g.node_pos(m_node)
            forced = {
                "name": m_name, "kind": "unridden", "node": m_node,
                "lat": m_lat, "lon": m_lon,
                "road_lat": pos[0], "road_lon": pos[1],
                "target_lat": pos[0], "target_lon": pos[1],
                "weight": 1.0, "preference": 1.0, "spot_density": 0.0,
                "anchor_score": 99.0,
                "t": secs.get(m_node, budget * 0.5),
                "bearing": bearing(origin_pos, pos),
                "dist": haversine_m(origin_pos, pos),
            }
            anchors = [forced] + list(anchors)

    attempts = 0
    for anchor in anchors:
        attempts += 1
        chain = _build_chain(g, cost, index, origin, anchor, cands, budget,
                             varied_alpha, max_stops, origin_pos, rider_id,
                             profile_ratings, rng, retrace)
        if chain:
            route = chain["routes"][0]
            route["spot_search"] = _spot_search_score(route, rider_id, budget)
            route["spot_search"]["alternatives_evaluated"] = attempts
            route["spot_search"]["candidate_pois_evaluated"] = len(cands)
            route["spot_search"]["anchor_density_score"] = round(
                anchor["spot_density"], 2)
            route["spot_search"]["variation_seed"] = variation_seed
            route["spot_search"]["scenic_pressure"] = round(varied_alpha, 3)
            route["spot_search"]["variation"] = (
                "quality-bounded randomized scenic shortlist")
            return {"routes": [route], "reason": None}
    return {"routes": [], "reason": "no chain fitted the budget"}


def _spot_search_score(route: dict, rider_id: str, budget_seconds: float) -> dict:
    """Score a completed route by the valuable POIs its road corridor visits."""
    visited = route.get("visited", [])
    cool = [poi for poi in visited
            if poi.get("rider_style_owner", "A") == rider_id]
    cool.sort(key=lambda poi: (-poi["cool_score"][rider_id], poi["distance_m"]))
    affinity_sum = sum(poi["cool_score"][rider_id] / 10.0 for poi in cool)
    objective_sum = sum(poi["objective_scenic"] / 10.0 for poi in cool)
    time_use = min(1.0, route["kpis"]["minutes"] * 60.0
                   / max(budget_seconds, 1.0))
    # Count dominates: a route must gain a genuinely good POI before a small
    # score difference can beat it. The remaining terms settle close calls.
    search_score = (10.0 * len(cool) + 2.0 * affinity_sum
                    + objective_sum + time_use)
    return {
        "mode": "personalized scenic orienteering",
        "cool_threshold": "exclusive top-30% style assignment",
        "cool_spot_count": len(cool),
        "all_pois_passed": len(visited),
        "search_score": round(search_score, 3),
        "blend": "65% rider affinity · 35% objective scenic quality",
        "top_spots": [{
            "name": poi["name"],
            "kind": poi["kind"],
            "score": round(poi["cool_score"][rider_id], 1),
            "rank": poi["rider_rank"][rider_id]["rank"],
            "distance_from_route_m": poi["distance_m"],
        } for poi in cool[:8]],
    }


def rank_loop_fallbacks(loops: list[dict], index: ScenicIndex, rider_id: str,
                        minutes: float, rider_profile: dict | None = None,
                        seed: int | str | None = None) -> list[dict]:
    """Personalize only genuine loops when no named scenic chain fits."""
    profile_ratings = (rider_profile or {}).get("style_ratings", {})
    strongest = sorted(profile_ratings.items(), key=lambda item: -item[1])[:3]
    # Very short city-origin rides can share one access stem while still being
    # a genuine loop. Keep the strict 10% rule normally, with a narrow 15%
    # allowance only for presets of 45 minutes or less.
    max_overlap = .15 if minutes <= 45 else .10
    safe = [route for route in loops
            if route.get("overlap", 1.0) <= max_overlap]
    for route in safe:
        route["visited"] = index.visited_by(route["coords"])
        route["spot_search"] = _spot_search_score(
            route, rider_id, minutes * 60.0)
        route["spot_search"].update({
            "variation_seed": seed,
            "variation": "quality-bounded personalized loop fallback",
            "candidate_pois_evaluated": len(index.pois),
            "alternatives_evaluated": len(safe),
        })
        route["personalization"] = {
            "rider": rider_id,
            "rider_type": RIDER_TYPES[rider_id],
            "strongest_traits": [{"name": name, "rating": rating}
                                 for name, rating in strongest],
            "selection": ("personalized cool spots along a hard-capped "
                          "low-overlap loop"),
        }
        top = route["spot_search"]["top_spots"]
        if top:
            route["label"] = f"Loop via {top[0]['name']}"
    safe.sort(key=lambda route: (
        -route["spot_search"]["search_score"],
        -float(route.get("score", 0.0))))
    # All candidates have already passed the loop and quality checks. Rotate
    # the best three reproducibly so a repeated joy ride need not be identical.
    if safe:
        variation_seed = (random.SystemRandom().randrange(2 ** 32)
                          if seed is None else seed)
        rng = random.Random(variation_seed)
        window = safe[:min(3, len(safe))]
        chosen = rng.randrange(len(window))
        safe = [window[chosen], *window[:chosen], *window[chosen + 1:],
                *safe[len(window):]]
        safe[0]["spot_search"]["variation_seed"] = variation_seed
    return safe


def _build_chain(g, cost, index, origin, anchor, cands, budget, alpha,
                 max_stops, origin_pos, rider_id="A", profile_ratings=None,
                 rng=None, retrace=None):
    """Greedy, monotone in distance from home, so the ride does not double back."""
    stops = [anchor]
    rng = rng or random.Random(0)
    ranked_cands = sorted(cands, key=lambda c: (
        -(c["preference"] + .035 * min(c.get("spot_density", 0.0), 8.0)
          + rng.uniform(-0.055, 0.055)),
        -c["weight"]))
    for c in ranked_cands:
        if len(stops) >= max_stops:
            break
        if c["node"] == anchor["node"]:
            continue
        # A second stop must be near the first -- otherwise it is a second ride,
        # not a second stop -- and must not send us back the way we came.
        d_from_last = haversine_m((stops[-1]["road_lat"], stops[-1]["road_lon"]),
                                  (c["road_lat"], c["road_lon"]))
        if not 3_000 <= d_from_last <= 28_000:
            continue
        if c["dist"] < stops[-1]["dist"] * 0.55:
            continue
        if any(haversine_m((s["road_lat"], s["road_lon"]),
                           (c["road_lat"], c["road_lon"])) < 2_500 for s in stops):
            continue
        stops.append(c)

    # Furthest point in the middle: ride out, work along, come home.
    stops.sort(key=lambda s: s["dist"])
    ordered = stops[1::2] + stops[::2][::-1] if len(stops) > 2 else stops

    return _ride(g, cost, index, origin, ordered, alpha, budget, max_stops,
                 rider_id, profile_ratings or {}, retrace)


def _ride(g, cost, index, origin, ordered, alpha, budget, max_stops,
          rider_id="A", profile_ratings=None, retrace=None):
    """Walk the chain: one goal-directed leg per stop, then home.

    Roads already used are charged four times over on later legs -- BMW's own
    `alreadyUsedRoads` -- so the way back is not a rerun of the way out.
    """
    if not ordered:
        return None
    segs: list[int] = []
    used: set[int] = set()
    retraced: set[int] = set()
    node = origin
    legs = []
    for s in ordered:
        part = _leg(g, cost, node, s["node"], alpha, used)
        if not part:
            return None
        retraced |= set(part) & used
        segs += part
        used |= set(part)
        legs.append({"to": s["name"], "kind": s["kind"],
                     "km": round(sum(float(g.by_id[x]["length_m"])
                                     for x in part) / 1000, 1)})
        node = s["node"]

    home = _leg(g, cost, node, origin, alpha, used)
    if not home:
        return None
    retraced |= set(home) & used
    segs += home
    legs.append({"to": "home", "kind": "return",
                 "km": round(sum(float(g.by_id[x]["length_m"])
                                 for x in home) / 1000, 1)})

    route = build(g, cost, segs, origin)
    retrace_m = sum(float(g.by_id[sid]["length_m"]) for sid in retraced)
    route_m = max(route["kpis"]["km"] * 1000.0, 1.0)
    retrace_share = retrace_m / route_m
    # Reject a destination-on-a-stick, but tolerate the short shared street
    # that can be unavoidable when leaving and returning to the same address.
    # The default limits keep an AUTO-GENERATED joy ride from being a
    # destination on a stick. When the rider has named the road themselves,
    # sharing more of it is the price of going where they asked, so the caller
    # can raise the bar -- but never to "any amount".
    lim_m, lim_share = retrace or (MAX_RETRACE_M, MAX_RETRACE_SHARE)
    if retrace_m > lim_m or retrace_share > lim_share:
        return None
    route["kpis"]["retrace_km"] = round(retrace_m / 1000.0, 1)
    route["kpis"]["retrace_share"] = round(retrace_share, 3)
    if route["kpis"]["minutes"] > budget / 60.0 * 1.12:
        # Too long. Drop the least valuable stop and try the shorter chain --
        # better a real ride to two places than no answer at all.
        if len(ordered) > 1:
            drop = min(ordered, key=lambda s: (s.get("preference", 0), s["weight"]))
            return _ride(g, cost, index, origin,
                         [s for s in ordered if s is not drop], alpha, budget,
                         max_stops, rider_id, profile_ratings or {}, retrace)
        return None

    route.update(_thirds(g, cost, segs))
    route["stops"] = [{"name": s["name"], "kind": s["kind"],
                       "lat": s["target_lat"], "lon": s["target_lon"],
                       "target_name": s["target_name"],
                       "target_kind": s["target_kind"],
                       "target_lat": s["target_lat"],
                       "target_lon": s["target_lon"],
                       "target_source": s["target_source"],
                       "rider_rating": s["rider_fit"][rider_id],
                       "rider_rank": s["rider_rank"][rider_id]["rank"]}
                      for s in ordered]
    route["legs"] = legs
    route["visited"] = index.visited_by(route["coords"])
    route["label"] = " \u00b7 ".join(s["name"] for s in ordered)
    strongest = sorted((profile_ratings or {}).items(), key=lambda item: -item[1])[:3]
    route["personalization"] = {
        "rider": rider_id,
        "rider_type": RIDER_TYPES[rider_id],
        "strongest_traits": [{"name": name, "rating": rating}
                             for name, rating in strongest],
        "selection": ("48% personalized cool score · 25% nearby spot density · "
                      "16% profile reach · 11% scenic weight"),
    }
    return {"routes": [route], "reason": None}
