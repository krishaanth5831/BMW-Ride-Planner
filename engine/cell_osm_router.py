"""The same distorted Dijkstra, but on roads that exist.

Routing on morton squares draws a staircase. Cell centres sit on a lattice, so
every step is 102 m north/south/east/west or 145 m diagonal, and a third of the
turns come out above 90 degrees -- the line reads as geometry, not as a road,
and can cut across fields. `precompute/fetch_osm.py` was written for exactly
this reason.

So: OSM supplies the geometry and the connectivity, and the **morton cell under
each segment still supplies the score**. The square stays the unit of analysis,
which is what made the original algorithm the original algorithm:

    cost(e) = time(e) * ( 1 + alpha*(1 - value(cell)) + beta*risk(cell) )

unchanged, hard lean exclusion unchanged, alpha still the one dial. All that
moves is where the line is drawn -- and now it cannot leave the road.

Segments whose square the crowd never rode are NOT excluded. They take the
neutral value of 0.5 and the regional speed, exactly as the confidence
shrinkage already does for a thinly-observed square. Dropping them would
reintroduce the connectivity problem in a new shape.
"""

from __future__ import annotations

import heapq
import json
import math
import os
from collections import defaultdict

from engine.cell_router import BETA, Cost
from precompute.build_osm_graph import (build_index, geometric_curviness,
                                        haversine_m, subdivide, ways_to_segments)
from precompute.morton import LEVEL_NODE, morton_code

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures", "osm")

# The tiles already cached in the repo. Munich and Kesselberg both sit inside
# it, so the flagship route needs no network at all. A click outside is
# refused rather than silently triggering an unbounded live Overpass fetch on
# somebody's click path.
BBOX = (47.60, 11.15, 48.25, 11.80)

# Keep the quick city escape, then strongly prefer riding roads once clear of
# Munich. The penalty ramps in over 5 km so Dijkstra does not chase a sharp
# artificial boundary at exactly 20 km.
MARIENPLATZ = (48.137154, 11.576124)
HIGHWAY_AVOID_START_KM = 20.0
HIGHWAY_AVOID_FULL_KM = 25.0
OUTER_HIGHWAY_COST = {
    "motorway": 6.0, "motorway_link": 5.0,
    "trunk": 4.0, "trunk_link": 3.0,
    "primary": 1.45, "primary_link": 1.25,
}
STRAIGHT_RUN_START_KM = 0.7
STRAIGHT_RUN_FULL_KM = 3.0
STRAIGHT_CURVE_FULL_DEG_PER_KM = 70.0
STRAIGHT_MAX_EXTRA_COST = 3.0

# ~2 km at this latitude. Big enough that one quiet street does not read as
# countryside, small enough that a town does not smear across a valley.
URBAN_CELL_DEG = 0.018

# Below this a place counts as out of town, and is allowed to host a
# turnaround.
RURAL_MAX = 0.18


class RoadGraph:
    """OSM segments as edges, OSM junctions as nodes, morton cells as scores."""

    def __init__(self, segments: list[dict], cells: CellLookup):
        self.segments = segments
        self._annotate_road_runs()
        self.by_id = {s["seg_id"]: s for s in segments}
        self.cells = cells
        self.adj: dict[int, list[int]] = defaultdict(list)
        for s in segments:
            self.adj[s["node_a"]].append(s["seg_id"])
            self.adj[s["node_b"]].append(s["seg_id"])
        self.degree = {n: len(v) for n, v in self.adj.items()}
        self.index = build_index(segments)
        self._pos: dict[int, tuple[float, float]] = {}
        for s in segments:
            g = s["geometry"]
            self._pos.setdefault(s["node_a"], (g[0][0], g[0][1]))
            self._pos.setdefault(s["node_b"], (g[-1][0], g[-1][1]))
        self._main = self._main_component()
        self._urban = self._urbanness()

    def _annotate_road_runs(self) -> None:
        """Attach the total named-road length to each ~100 m chunk."""
        totals = defaultdict(float)
        keys = []
        for seg in self.segments:
            key = (("name", seg["name"], seg.get("highway")) if seg.get("name")
                   else ("way", seg.get("way_id")))
            keys.append(key)
            totals[key] += float(seg.get("length_m") or 0.0)
        for seg, key in zip(self.segments, keys):
            seg["road_run_km"] = totals[key] / 1000.0

    def _main_component(self) -> set[int]:
        """The biggest connected piece of the network.

        A bbox clips roads at its edge, leaving stubs and small islands. A map
        click near the boundary otherwise snaps to a dead end that no route can
        ever reach -- Kesselberg sits on the southern edge and did exactly
        that, giving "no route" for a road that is plainly there.
        """
        seen: set[int] = set()
        best: set[int] = set()
        for root in self.adj:
            if root in seen:
                continue
            comp, stack = {root}, [root]
            seen.add(root)
            while stack:
                node = stack.pop()
                for sid in self.adj.get(node, ()):
                    nxt = self.other_end(self.by_id[sid], node)
                    if nxt not in seen:
                        seen.add(nxt)
                        comp.add(nxt)
                        stack.append(nxt)
            if len(comp) > len(best):
                best = comp
        return best

    def _urbanness(self) -> dict:
        """How built-up each place is, 0 rural to 1 city centre.

        Junction density is the signal, because it is the only one that works
        where it matters: the crowd never rode the Munich centre square at all,
        so crowd speed is simply absent there. Junctions per ~2 km cell,
        measured on this graph: Munich centre 223, Wolfratshausen 68,
        Kesselberg 0. Normalised against the graph's own busiest cell and
        cached per node, because recomputing it inside Dijkstra would cost more
        than the search.
        """
        deg3 = defaultdict(int)
        for node, (lat, lon) in self._pos.items():
            if self.degree.get(node, 0) >= 3:
                deg3[self._grid(lat, lon)] += 1
        if not deg3:
            return {}
        # A handful of freak cells should not set the scale for everything
        # else, so normalise against the 95th percentile, not the maximum.
        counts = sorted(deg3.values())
        top = max(counts[int(0.95 * (len(counts) - 1))], 1)
        return {k: min(1.0, v / top) for k, v in deg3.items()}

    @staticmethod
    def _grid(lat: float, lon: float) -> tuple[int, int]:
        return (int(lat / URBAN_CELL_DEG), int(lon / (URBAN_CELL_DEG * 1.5)))

    def urban_at(self, lat: float, lon: float) -> float:
        return self._urban.get(self._grid(lat, lon), 0.0)

    def urban_of_node(self, node: int) -> float:
        return self.urban_at(*self.node_pos(node))

    def other_end(self, seg: dict, node: int) -> int:
        return seg["node_b"] if node == seg["node_a"] else seg["node_a"]

    def node_pos(self, node: int):
        return self._pos.get(node, (0.0, 0.0))

    def cell_of(self, seg: dict) -> dict:
        return self.cells.at(*seg["mid"])

    def seconds(self, seg: dict) -> float:
        v = self.cells.speed_of(seg)
        return (float(seg["length_m"]) / 1000.0) / max(v, 3.0) * 3600.0

    def nearest_node(self, lat: float, lon: float) -> int | None:
        """Snap to the road network.

        `build_osm_graph.snap` gives up beyond SNAP_MAX_M, which is right for
        matching a GPS trace to a road but wrong for a map click: residential
        and service roads are deliberately not in this graph, so a click in a
        city centre is often more than 45 m from the nearest rideable road.
        A map click should land on the nearest road there is, not fail.
        """
        best, best_d = None, float("inf")
        k = math.cos(math.radians(lat))
        for node, (nlat, nlon) in self._pos.items():
            if node not in self._main:
                continue
            d = (nlat - lat) ** 2 + ((nlon - lon) * k) ** 2
            if d < best_d:
                best, best_d = node, d
        return best

    def coverage(self) -> float:
        """Share of road segments whose square the crowd actually rode."""
        n = sum(1 for s in self.segments if self.cells.at(*s["mid"]) is not None)
        return n / max(len(self.segments), 1)


class CellLookup:
    """morton square -> crowd score, for any point on the road network."""

    def __init__(self, blob: dict):
        self.cells: dict[str, dict] = blob["cells"]
        self.region_speed = blob.get("region_speed_kmh", 40.0)
        self._cache: dict[tuple, dict | None] = {}

    def at(self, lat: float, lon: float):
        key = (round(lat, 5), round(lon, 5))
        if key not in self._cache:
            self._cache[key] = self.cells.get(morton_code(lat, lon, LEVEL_NODE))
        return self._cache[key]

    def speed_of(self, seg: dict) -> float:
        cell = self.at(*seg["mid"])
        if cell and (cell.get("speed_p85") or 0) > 0:
            return float(cell["speed_p85"])
        # No crowd here: fall back to the class's own free-flow speed rather
        # than to the regional median, which would price an empty motorway the
        # same as an empty village lane.
        return CLASS_SPEED.get(seg.get("highway"), self.region_speed)


CLASS_SPEED = {
    "motorway": 120.0, "motorway_link": 70.0, "trunk": 100.0, "trunk_link": 60.0,
    "primary": 80.0, "primary_link": 50.0, "secondary": 70.0,
    "secondary_link": 45.0, "tertiary": 60.0, "tertiary_link": 40.0,
    "unclassified": 45.0,
}


class RoadCost:
    """The identical distortion, reading its terms from the square below."""

    def __init__(self, graph: RoadGraph, cost: Cost, escape: bool = True,
                 outer_highway_avoidance: float = 1.0,
                 straight_avoidance: float = 1.0):
        self.g = graph
        self.c = cost                       # the cell Cost, reused verbatim
        self.escape = escape
        self.outer_highway_avoidance = max(0.0, outer_highway_avoidance)
        self.straight_avoidance = max(0.0, straight_avoidance)

    def value(self, seg: dict) -> float:
        cell = self.g.cell_of(seg)
        if cell is None:
            return 0.5                      # unridden reads neutral, not bad
        return self.c.value(morton_code(*seg["mid"], LEVEL_NODE), cell)

    def risk(self, seg: dict) -> float:
        cell = self.g.cell_of(seg)
        return 0.1 if cell is None else self.c.risk(cell)

    def excluded(self, seg: dict) -> bool:
        cell = self.g.cell_of(seg)
        return False if cell is None else self.c.excluded(cell)

    def alpha_at(self, seg: dict, alpha: float) -> float:
        """Spend the detour budget where there is something to detour for.

        A city has no scenic road to find, so paying alpha inside one just buys
        a slower way through traffic. Fading alpha out with urbanness makes the
        SAME search do what a rider actually wants -- shortest way out of town,
        then the long way round once there is countryside to enjoy, then home.
        No phases, no second engine: the shape falls out of one Dijkstra.

        The four properties hold unchanged. alpha_eff >= 0, so the multiplier
        is still >= 1, every arc is still strictly positive, and alpha is still
        the one dial -- it just does not apply where it would be wasted.
        """
        if not self.escape:
            return alpha
        return alpha * (1.0 - self.g.urban_at(*seg["mid"]))

    def edge_cost(self, seg: dict, alpha: float) -> float:
        t = self.g.seconds(seg)
        a = self.alpha_at(seg, alpha)
        base = t * (1.0 + a * (1.0 - self.value(seg)) + BETA * self.risk(seg))
        return base * self.highway_factor(seg) * self.straight_factor(seg)

    def highway_factor(self, seg: dict) -> float:
        """Softly avoid major roads beyond 20 km from Marienplatz."""
        target = OUTER_HIGHWAY_COST.get(seg.get("highway"), 1.0)
        if target == 1.0 or self.outer_highway_avoidance == 0.0:
            return 1.0
        distance_km = haversine_m(MARIENPLATZ, tuple(seg["mid"])) / 1000.0
        ramp = max(0.0, min(1.0, (distance_km - HIGHWAY_AVOID_START_KM)
                                / (HIGHWAY_AVOID_FULL_KM - HIGHWAY_AVOID_START_KM)))
        return 1.0 + ramp * self.outer_highway_avoidance * (target - 1.0)

    def straight_factor(self, seg: dict) -> float:
        """Penalise long, locally straight roads outside the Munich radius."""
        if self.straight_avoidance == 0.0:
            return 1.0
        distance_km = haversine_m(MARIENPLATZ, tuple(seg["mid"])) / 1000.0
        city_ramp = max(0.0, min(1.0, (distance_km - HIGHWAY_AVOID_START_KM)
                                     / (HIGHWAY_AVOID_FULL_KM - HIGHWAY_AVOID_START_KM)))
        run_km = float(seg.get("road_run_km") or 0.0)
        run_pressure = max(0.0, min(1.0, (run_km - STRAIGHT_RUN_START_KM)
                                        / (STRAIGHT_RUN_FULL_KM - STRAIGHT_RUN_START_KM)))
        curve = max(0.0, float(seg.get("curvature_geo") or 0.0))
        straightness = 1.0 - min(1.0, curve / STRAIGHT_CURVE_FULL_DEG_PER_KM)
        return 1.0 + (city_ramp * run_pressure * straightness
                      * self.straight_avoidance * STRAIGHT_MAX_EXTRA_COST)


def dijkstra(g: RoadGraph, cost: RoadCost, start: int, alpha: float, *,
             goal: int | None = None, max_seconds: float | None = None,
             reuse: set | None = None):
    reuse = reuse or set()
    dist = {start: 0.0}
    secs = {start: 0.0}
    prev_seg: dict[int, int] = {}
    prev_node: dict[int, int] = {}
    heap = [(0.0, start, -1)]
    while heap:
        d, u, via = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue
        if goal is not None and u == goal:
            break
        for sid in g.adj.get(u, ()):
            if sid == via:
                continue                    # no immediate U-turn
            seg = g.by_id[sid]
            if cost.excluded(seg):
                continue
            v = g.other_end(seg, u)
            if v == u:
                continue
            step = cost.edge_cost(seg, alpha)
            if sid in reuse:
                step *= 4.0
            ns = secs[u] + g.seconds(seg)
            if max_seconds is not None and ns > max_seconds:
                continue
            nd = d + step
            if nd < dist.get(v, float("inf")):
                dist[v], secs[v] = nd, ns
                prev_seg[v], prev_node[v] = sid, u
                heapq.heappush(heap, (nd, v, sid))
    return dist, secs, prev_seg, prev_node


def path_segments(prev_seg, prev_node, start: int, end: int) -> list[int]:
    out, cur, guard = [], end, 0
    while cur != start and cur in prev_seg and guard < 500_000:
        out.append(prev_seg[cur])
        cur = prev_node[cur]
        guard += 1
    if cur != start:
        return []
    out.reverse()
    return out


def build(g: RoadGraph, cost: RoadCost, seg_ids: list[int], origin: int) -> dict:
    """Real way geometry, oriented start to finish, plus the KPIs."""
    coords: list[list[float]] = []
    node = origin
    total_m = total_s = 0.0
    val = rsk = lean = 0.0
    elev: list[float] = []
    names: list[str] = []
    on_crowd_m = highway_m = outer_highway_m = long_straight_m = 0.0
    for sid in seg_ids:
        seg = g.by_id[sid]
        geom = seg["geometry"]
        if node == seg["node_b"]:
            geom = list(reversed(geom))
        if coords and coords[-1] == [geom[0][0], geom[0][1]]:
            geom = geom[1:]
        coords.extend([[p[0], p[1]] for p in geom])
        L = float(seg["length_m"])
        total_m += L
        if seg.get("highway") in ("motorway", "motorway_link", "trunk", "trunk_link"):
            highway_m += L
            if haversine_m(MARIENPLATZ, tuple(seg["mid"])) / 1000.0 > HIGHWAY_AVOID_START_KM:
                outer_highway_m += L
        if (float(seg.get("road_run_km") or 0.0) >= STRAIGHT_RUN_START_KM
                and float(seg.get("curvature_geo") or 0.0) < 25.0
                and haversine_m(MARIENPLATZ, tuple(seg["mid"])) / 1000.0
                    > HIGHWAY_AVOID_START_KM):
            long_straight_m += L
        total_s += g.seconds(seg)
        val += cost.value(seg) * L
        rsk += cost.risk(seg) * L
        cell = g.cell_of(seg)
        if cell is not None:
            on_crowd_m += L
            lean += float(cell.get("lean_p50") or 0.0) * L
            if cell.get("elev_m") is not None:
                elev.append(float(cell["elev_m"]))
        if seg.get("name"):
            names.append(seg["name"])
        node = g.other_end(seg, node)

    den = max(total_m, 1e-6)
    win = 9
    sm = [sum(elev[max(0, i - win // 2):i + win // 2 + 1])
          / len(elev[max(0, i - win // 2):i + win // 2 + 1])
          for i in range(len(elev))] if len(elev) >= win else elev
    gain = sum(d for i in range(1, len(sm)) if (d := sm[i] - sm[i - 1]) > 1.0)

    seen, roads = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            roads.append(n)
    return {
        "coords": coords,
        "kpis": {
            "km": round(total_m / 1000.0, 1),
            "minutes": round(total_s / 60.0, 1),
            "squares": len(seg_ids),
            "value": round(val / den, 3),
            "risk": round(rsk / den, 3),
            "mean_lean_deg": round(lean / max(on_crowd_m, 1e-6), 1),
            "elev_gain_m": round(gain, 0),
            "crowd_covered_pct": round(100 * on_crowd_m / den, 0),
            "highway_km": round(highway_m / 1000.0, 1),
            "outer_highway_km": round(outer_highway_m / 1000.0, 1),
            "long_straight_km": round(long_straight_m / 1000.0, 1),
        },
        "roads": roads[:8],
    }


def plan_destination(g: RoadGraph, cost: RoadCost, start: int, goal: int,
                     alphas=(0.0, 1.5, 3.0)) -> list[dict]:
    out = []
    for label, alpha in zip(("Chill", "Balanced", "Full Send"), alphas):
        _d, _s, ps, pn = dijkstra(g, cost, start, alpha, goal=goal)
        segs = path_segments(ps, pn, start, goal)
        if not segs:
            continue
        r = build(g, cost, segs, start)
        r["label"], r["alpha"] = label, alpha
        out.append(r)
    return out


def bearing(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _thirds(g: RoadGraph, cost: RoadCost, seg_ids: list[int]) -> dict:
    """Value of the first, middle and last third of the ride, by distance.

    This is the number that says whether the escape actually worked. A loop
    that dawdles through the suburbs and one that sprints out to a pass and
    plays there can have the same average value; only the middle third tells
    them apart.
    """
    total = sum(float(g.by_id[s]["length_m"]) for s in seg_ids) or 1.0
    acc = 0.0
    buckets = [[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]   # [value*m, m]
    urban_m = 0.0
    for sid in seg_ids:
        seg = g.by_id[sid]
        L = float(seg["length_m"])
        i = min(2, int(3 * (acc + L / 2) / total))
        buckets[i][0] += cost.value(seg) * L
        buckets[i][1] += L
        urban_m += g.urban_at(*seg["mid"]) * L
        acc += L
    v = [round(b[0] / b[1], 3) if b[1] else 0.0 for b in buckets]
    return {"value_thirds": v, "middle_value": v[1],
            "urban_share": round(urban_m / total, 2)}


def joyride(g: RoadGraph, cost: RoadCost, start: int, minutes: float,
            alpha: float = 3.0, k: int = 3) -> list[dict]:
    """Mode 2, unchanged in shape -- flood, ring, bearings, out and back."""
    budget = minutes * 60.0
    half = budget / 2.0
    _d, secs, ps, pn = dijkstra(g, cost, start, alpha, max_seconds=half * 1.1)
    origin = g.node_pos(start)
    # Turn around near the far edge of what half the budget reaches. Sitting
    # the ring at 0.40 of it wasted most of the budget: from Munich a 4-hour
    # ask came back as a 112-minute suburban potter.
    ring = [(n, t) for n, t in secs.items() if 0.62 * half <= t <= 1.0 * half]
    if not ring:
        return []

    # A turnaround has to be somewhere worth turning around AT. From a city
    # origin the time ring lands in the suburbs -- Munich's built-up radius is
    # about 10 km, and 20 minutes of city-speed travel does not clear it -- so
    # a ring picked on time alone buys a fast escape into nothing.
    rural = [(n, t) for n, t in ring if g.urban_of_node(n) <= RURAL_MAX]
    escaped = bool(rural)
    if rural:
        ring = rural

    buckets: dict[int, list] = defaultdict(list)
    for node, t in ring:
        buckets[int(bearing(origin, g.node_pos(node)) // 45)].append((node, t))

    loops = []
    for _b, items in buckets.items():
        items.sort(key=lambda x: -x[1])
        turn = items[0][0]
        out_segs = path_segments(ps, pn, start, turn)
        if not out_segs:
            continue
        used = set(out_segs)
        out_s = sum(g.seconds(g.by_id[s]) for s in out_segs)
        _d2, _s2, ps2, pn2 = dijkstra(g, cost, turn, alpha, goal=start,
                                      reuse=used,
                                      max_seconds=max(budget - out_s, 60.0) * 1.25)
        back = path_segments(ps2, pn2, turn, start)
        if not back:
            continue
        r = build(g, cost, out_segs + back, start)
        if r["kpis"]["km"] < 5 or r["kpis"]["minutes"] > minutes * 1.08:
            continue
        r["bearing"] = round(bearing(origin, g.node_pos(turn)))
        r["overlap"] = round(len(used & set(back)) / max(len(used), 1), 2)
        r["turnaround_urban"] = round(g.urban_of_node(turn), 2)
        r["escaped_town"] = escaped
        r.update(_thirds(g, cost, out_segs + back))
        # Rank on what the middle is worth, not the average. Averaging the
        # scenic middle together with the two dull legs out and back is what
        # makes every loop look the same.
        # What makes a joyride good is the quality of the part that is not
        # commuting, and how little of the ride is spent in traffic. Dividing
        # value by hours -- the obvious "value per hour" -- ranks a short
        # potter above a real ride, which is the opposite of what someone
        # asking for three hours out wants.
        used = min(1.0, r["kpis"]["minutes"] / minutes)
        r["score"] = (r["middle_value"]
                      * (1.0 - 0.5 * r["urban_share"])
                      * (0.35 + 0.65 * used))
        loops.append(r)

    loops.sort(key=lambda r: -r["score"])
    for i, r in enumerate(loops[:k]):
        r["label"] = ["Round trip", "Alternative", "Long way round"][i]
    return loops[:k]


def load_ways(bbox=BBOX) -> list[dict]:
    """Cached Overpass tiles only. A miss is fetched once, politely, by
    `python3 -m precompute.fetch_osm` -- never on a request path."""
    ways: dict[int, dict] = {}
    for f in sorted(os.listdir(FIXTURES)):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES, f)) as fh:
            blob = json.load(fh)
        for w in (blob if isinstance(blob, list) else blob.get("elements", [])):
            if w.get("type") == "way" or "geometry" in w:
                ways[w.get("id", len(ways))] = w
    return list(ways.values())


def load_blob(blob: dict) -> RoadGraph:
    """Build the road graph from an in-memory crowd layer.

    A neutral layer is useful for the lightweight demo: OSM supplies real road
    geometry while missing telemetry follows the existing neutral fallbacks.
    """
    cells = CellLookup(blob)
    base_segments = ways_to_segments(load_ways())
    for segment in base_segments:
        segment["curvature_geo"] = geometric_curviness(segment["geometry"])
    segments = subdivide(base_segments)
    for s in segments:
        pts = s["geometry"]
        mid = pts[len(pts) // 2]
        s["mid"] = (float(mid[0]), float(mid[1]))
    return RoadGraph(segments, cells)


def load(cell_graph_path: str) -> RoadGraph:
    with open(cell_graph_path) as fh:
        return load_blob(json.load(fh))
