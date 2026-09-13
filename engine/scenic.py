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

from engine.cell_osm_router import (RURAL_MAX, RoadCost, RoadGraph, _thirds,
                                    bearing, build, dijkstra, path_segments)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENIC_DIR = os.path.join(ROOT, "fixtures", "scenic")

# South of Munich is where the lakes and the Alps are. When the ride starts in
# a city we aim the whole chain into that half of the compass rather than
# letting it wander north into the flatland -- a candidate FILTER, so the cost
# function and its four properties are untouched.
# Kept deliberately narrow. A 100-260 sector still admits the ponds on the
# eastern edge of Munich, which is how a "scenic ride" came back as 88% inside
# the city. The lakes and the Alps are genuinely south to south-west.
SOUTH_SECTOR = (140.0, 235.0)

# A POI is "visited" if the route passes this close. Riding past the far shore
# of a lake still counts as riding to the lake.
VISIT_M = 700.0
LAKE_VIEWPOINT_MAX_M = 8_000.0


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
            self.pois.append({**p, "node": node, "road_lat": pos[0],
                              "road_lon": pos[1],
                              "target_name": target["name"],
                              "target_kind": target["kind"],
                              "target_lat": display_lat,
                              "target_lon": display_lon,
                              "source_lat": target["lat"],
                              "source_lon": target["lon"],
                              "snap_distance_m": snap_distance_m,
                              "target_source": target_source + " · OSM road"})

    def visited_by(self, coords: list[list[float]], within_m: float = VISIT_M):
        """Which POIs the finished route actually passes. The honest check."""
        if not coords:
            return []
        # Thin the polyline before the O(n*m) sweep; 2287 points against 600
        # POIs is slow and pointless at 100 m resolution.
        pts = coords[::3] or coords
        out = []
        for p in self.pois:
            best = min(haversine_m((p["target_lat"], p["target_lon"]),
                                   (c[0], c[1])) for c in pts)
            if best <= within_m:
                out.append({"name": p["name"], "kind": p["kind"],
                            "lat": p["target_lat"], "lon": p["target_lon"],
                            "weight": p["weight"],
                            "target_name": p["target_name"],
                            "target_kind": p["target_kind"],
                            "target_lat": p["target_lat"],
                            "target_lon": p["target_lon"],
                            "target_source": p["target_source"],
                            "distance_m": round(best)})
        out.sort(key=lambda q: -q["weight"])
        return out


def _leg(g: RoadGraph, cost: RoadCost, a: int, b: int, alpha: float, used: set):
    _d, _s, ps, pn = dijkstra(g, cost, a, alpha, goal=b, reuse=used)
    return path_segments(ps, pn, a, b)


def plan_scenic_loop(g: RoadGraph, cost: RoadCost, index: ScenicIndex,
                     origin: int, minutes: float, alpha: float = 3.0,
                     max_stops: int = 3, south_bias: bool | None = None) -> dict:
    """A ride out to real places and back, inside the time budget."""
    budget = minutes * 60.0
    origin_pos = g.node_pos(origin)

    if south_bias is None:
        # Only aim a city rider south. Somebody already in the foothills has
        # good roads in every direction and should not be herded.
        south_bias = g.urban_of_node(origin) > RURAL_MAX
    sector = SOUTH_SECTOR if south_bias else None

    # One flood gives travel time to every POI at once.
    _d, secs, ps, pn = dijkstra(g, cost, origin, alpha, max_seconds=budget * 0.62)

    cands = []
    for p in index.pois:
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
                      "dist": haversine_m(origin_pos, (p["road_lat"], p["road_lon"]))})
    if not cands:
        return {"routes": [], "reason":
                "Nothing worth riding to is reachable in this time"
                + (", heading south out of the city." if sector else ".")
                + " Give it longer and the lakes come into range."}

    # The anchor is the furthest thing worth riding to that still leaves time
    # to get home: weight matters, but so does not spending the whole budget
    # on the outbound leg.
    for c in cands:
        reach = c["t"] / max(budget * 0.5, 1.0)
        c["anchor_score"] = c["weight"] * (1.0 - abs(reach - 0.85))
    cands.sort(key=lambda c: -c["anchor_score"])

    for anchor in cands[:6]:
        chain = _build_chain(g, cost, index, origin, anchor, cands, budget,
                             alpha, max_stops, origin_pos)
        if chain:
            return chain
    return {"routes": [], "reason": "no chain fitted the budget"}


def _build_chain(g, cost, index, origin, anchor, cands, budget, alpha,
                 max_stops, origin_pos):
    """Greedy, monotone in distance from home, so the ride does not double back."""
    stops = [anchor]
    for c in sorted(cands, key=lambda c: -c["weight"]):
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

    return _ride(g, cost, index, origin, ordered, alpha, budget, max_stops)


def _ride(g, cost, index, origin, ordered, alpha, budget, max_stops):
    """Walk the chain: one goal-directed leg per stop, then home.

    Roads already used are charged four times over on later legs -- BMW's own
    `alreadyUsedRoads` -- so the way back is not a rerun of the way out.
    """
    if not ordered:
        return None
    segs: list[int] = []
    used: set[int] = set()
    node = origin
    legs = []
    for s in ordered:
        part = _leg(g, cost, node, s["node"], alpha, used)
        if not part:
            return None
        segs += part
        used |= set(part)
        legs.append({"to": s["name"], "kind": s["kind"],
                     "km": round(sum(float(g.by_id[x]["length_m"])
                                     for x in part) / 1000, 1)})
        node = s["node"]

    home = _leg(g, cost, node, origin, alpha, used)
    if not home:
        return None
    segs += home
    legs.append({"to": "home", "kind": "return",
                 "km": round(sum(float(g.by_id[x]["length_m"])
                                 for x in home) / 1000, 1)})

    route = build(g, cost, segs, origin)
    if route["kpis"]["minutes"] > budget / 60.0 * 1.12:
        # Too long. Drop the least valuable stop and try the shorter chain --
        # better a real ride to two places than no answer at all.
        if len(ordered) > 1:
            drop = min(ordered, key=lambda s: s["weight"])
            return _ride(g, cost, index, origin,
                         [s for s in ordered if s is not drop], alpha, budget,
                         max_stops)
        return None

    route.update(_thirds(g, cost, segs))
    route["stops"] = [{"name": s["name"], "kind": s["kind"],
                       "lat": s["target_lat"], "lon": s["target_lon"],
                       "target_name": s["target_name"],
                       "target_kind": s["target_kind"],
                       "target_lat": s["target_lat"],
                       "target_lon": s["target_lon"],
                       "target_source": s["target_source"]} for s in ordered]
    route["legs"] = legs
    route["visited"] = index.visited_by(route["coords"])
    route["label"] = " \u00b7 ".join(s["name"] for s in ordered)
    return {"routes": [route], "reason": None}
