"""Dijkstra over the OSM segment graph, with both ride modes.

  * Destination (A -> B): plain Dijkstra, alpha binary-searched to hit the
    requested duration.
  * Round trip: one Dijkstra used four times -- flood out, take the ring at
    half the budget, spread turnarounds across bearings, route back with a
    reuse penalty (BMW's own `alreadyUsedRoads`).

Because the graph is real OSM way geometry, a route cannot leave the road
network. The previous crowd-only graph joined cell centroids with straight
lines, which is why routes cut across open land.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict

# A junction only costs you if you stop or turn. Passing a side road on
# priority is free -- which is what stops "fewest junctions" from quietly
# becoming a motorway-seeking objective, since motorways have almost no
# at-grade junctions. Values are SECONDS, so they stay commensurate with travel
# time and cannot blow up into absurd detours.
TURN_ATTENTION_S = 5.0
JUNCTION_DEGREE_S = 3.0
MOTORWAY_TYPES = {"motorway", "motorway_link"}


def bearing_deg(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Graph:
    def __init__(self, segments: list[dict], adjacency: dict):
        self.segments = segments
        self.by_id = {s["seg_id"]: s for s in segments}
        self.adj = {int(k): v for k, v in adjacency.items()}
        self.degree = {n: len(v) for n, v in self.adj.items()}
        self._node_pos: dict[int, tuple[float, float]] = {}
        for s in segments:
            g = s["geometry"]
            self._node_pos.setdefault(s["node_a"], (g[0][0], g[0][1]))
            self._node_pos.setdefault(s["node_b"], (g[-1][0], g[-1][1]))

    def node_pos(self, node: int):
        return self._node_pos.get(node, (0.0, 0.0))

    def other_end(self, seg: dict, node: int) -> int:
        return seg["node_b"] if node == seg["node_a"] else seg["node_a"]

    def traversable(self, seg: dict, from_node: int) -> bool:
        """Respect one-ways: an oneway segment is only usable a -> b."""
        if seg.get("oneway") and from_node != seg["node_a"]:
            return False
        return True

    def junction_cost(self, node: int, turning: bool) -> float:
        if not turning:
            return 0.0
        return TURN_ATTENTION_S + JUNCTION_DEGREE_S * max(0, self.degree.get(node, 2) - 2)

    def nearest_node(self, lat: float, lon: float, require_degree: int = 1):
        best, best_d = None, float("inf")
        for node, (nlat, nlon) in self._node_pos.items():
            if self.degree.get(node, 0) < require_degree:
                continue
            d = (nlat - lat) ** 2 + ((nlon - lon) * math.cos(math.radians(lat))) ** 2
            if d < best_d:
                best, best_d = node, d
        return best

    # ------------------------------------------------------------------
    def dijkstra(self, start: int, scorer, alpha: float, *, goal=None,
             weather: float = 0.0, weather_factor: float = 1.0,
             max_seconds=None, avoid=None, blocked_highway_types=None):
        avoid = avoid or set()
        blocked_highway_types = blocked_highway_types or set()
        dist = {start: 0.0}
        elapsed = {start: 0.0}
        prev_seg: dict[int, int] = {}
        prev_node: dict[int, int] = {}
        heap = [(0.0, start, -1)]
        while heap:
            d, node, via = heapq.heappop(heap)
            if d > dist.get(node, float("inf")):
                continue
            if goal is not None and node == goal:
                break
            for seg_id in self.adj.get(node, ()):
                if seg_id == via:
                    continue            # no immediate doubling back
                seg = self.by_id[seg_id]
                if seg.get("highway") in blocked_highway_types:
                    continue
                if not self.traversable(seg, node):
                    continue
                if scorer.excluded(seg, weather_factor):
                    continue
                nxt = self.other_end(seg, node)
                if nxt == node:
                    continue
                jc = self.junction_cost(node, via != -1)
                step = scorer.cost(seg, alpha, weather=weather, junction_s=jc)
                if seg_id in avoid:
                    step += scorer.seconds(seg) * 4.0
                nd = d + step
                ne = elapsed[node] + scorer.seconds(seg) + jc
                if max_seconds is not None and ne > max_seconds:
                    continue
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    elapsed[nxt] = ne
                    prev_seg[nxt] = seg_id
                    prev_node[nxt] = node
                    heapq.heappush(heap, (nd, nxt, seg_id))
        return dist, prev_seg, prev_node, elapsed

    def path_segments(self, prev_seg, prev_node, start, end):
        out, cur, guard = [], end, 0
        while cur != start and cur in prev_seg and guard < 200_000:
            out.append(prev_seg[cur])
            cur = prev_node[cur]
            guard += 1
        out.reverse()
        return out if cur == start else []


# ----------------------------------------------------------------------
def build_route(graph: Graph, scorer, seg_ids, origin: int) -> dict:
    coords: list[list[float]] = []
    node = origin
    total_m = total_s = ridden_m = 0.0
    lean_delta = leaned_m = abs_events = 0.0
    scenic_w = risk_w = conf_w = 0.0
    junctions = 0
    elev: list[float] = []
    names: list[str] = []
    covered_m = 0.0

    for sid in seg_ids:
        seg = graph.by_id[sid]
        geom = seg["geometry"]
        if node == seg["node_b"]:
            geom = list(reversed(geom))
        if coords and coords[-1] == [geom[0][0], geom[0][1]]:
            geom = geom[1:]
        coords.extend([[p[0], p[1]] for p in geom])

        L = float(seg["length_m"])
        total_m += L
        total_s += scorer.seconds(seg)
        ridden_m += float(seg.get("path_m") or 0.0)
        lean_delta += float(seg.get("lean_delta") or 0.0)
        leaned_m += float(seg.get("leaned_m") or 0.0)
        abs_events += float(seg.get("abs_events") or 0.0)
        # Length-weighted, so a long motorway stretch cannot hide behind a short
        # pretty lane.
        scenic_w += scorer.scenic(seg) * L
        risk_w += scorer.risk(seg) * L
        conf_w += scorer.confidence(seg) * L
        if seg.get("n_trips"):
            covered_m += L
        if seg.get("elev_mean"):
            elev.append(float(seg["elev_mean"]))
        if seg.get("name"):
            names.append(seg["name"])
        node = graph.other_end(seg, node)
        if graph.degree.get(node, 2) != 2:
            junctions += 1

    elev_gain = sum(max(0.0, elev[i] - elev[i - 1]) for i in range(1, len(elev)))
    km = max(total_m / 1000.0, 1e-6)
    denom = max(total_m, 1e-6)

    # Named roads, most-travelled first -- so the UI can say "via the B11".
    seen: dict[str, float] = defaultdict(float)
    for sid in seg_ids:
        s = graph.by_id[sid]
        if s.get("name"):
            seen[s["name"]] += float(s["length_m"])
    via = [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:5]]

    return {
        "coords": coords,
        "seg_ids": seg_ids,
        "via": via,
        "kpis": {
            "km": round(km, 1),
            "minutes": round(total_s / 60.0, 1),
            "scenic": round(scenic_w / denom, 3),
            "risk": round(risk_w / denom, 3),
            "confidence": round(conf_w / denom, 3),
            "crowd_covered_pct": round(100 * covered_m / denom, 0),
            "curviness": round(lean_delta / max(ridden_m / 1000.0, 1e-6), 1) if ridden_m else 0.0,
            "leaned_share": round(leaned_m / ridden_m, 3) if ridden_m else 0.0,
            "elev_gain_m": round(elev_gain, 0),
            "junctions": junctions,
            "segments": len(seg_ids),
        },
    }


def _target_duration(graph, scorer, plan_fn, minutes: float, tol: float = 0.12):
    """Binary-search alpha so the route lands near the requested duration.

    Higher alpha buys a more scenic, longer route. Duration steps rather than
    curving smoothly because paths are discrete, so this is a search for a good
    enough alpha, not an exact solve.
    """
    lo, hi = 0.0, 12.0
    best = None
    for _ in range(7):
        mid = (lo + hi) / 2
        got = plan_fn(mid)
        if not got:
            hi = mid
            continue
        mins = got["kpis"]["minutes"]
        if best is None or abs(mins - minutes) < abs(best[0] - minutes):
            best = (mins, mid, got)
        if abs(mins - minutes) <= tol * minutes:
            break
        if mins < minutes:
            lo = mid
        else:
            hi = mid
    return best


def plan_ab(graph: Graph, scorer, start: int, goal: int, minutes: float | None,
            weather: float = 0.0, weather_factor: float = 1.0, k: int = 3):
    """A -> B with motorway-free scenic routes whenever possible."""

    def one(alpha: float, *, avoid_motorways: bool = False):
        blocked = MOTORWAY_TYPES if avoid_motorways else None

        _d, ps, pn, _el = graph.dijkstra(
            start,
            scorer,
            alpha,
            goal=goal,
            weather=weather,
            weather_factor=weather_factor,
            blocked_highway_types=blocked,
        )

        segs = graph.path_segments(ps, pn, start, goal)
        if not segs:
            return None

        route = build_route(graph, scorer, segs, start)
        route["alpha"] = round(alpha, 2)
        route["motorway_fallback"] = False
        return route

    def scenic_one(alpha: float):
        # First attempt: motorways are completely forbidden.
        route = one(alpha, avoid_motorways=True)
        if route is not None:
            return route

        # Fallback: allow motorways only when no motorway-free route exists.
        route = one(alpha, avoid_motorways=False)
        if route is not None:
            route["motorway_fallback"] = True
        return route

    # The fastest option is still allowed to use motorways.
    fastest = one(0.0)
    if fastest is None:
        return []

    out = [dict(fastest, label="Fastest")]

    def add(route, label):
        if not route:
            return

        # Do not display the same route under multiple names.
        if any(route["seg_ids"] == existing["seg_ids"] for existing in out):
            return

        if route.get("motorway_fallback"):
            label += " (motorway required)"

        out.append(dict(route, label=label))

    if minutes:
        found = _target_duration(graph, scorer, scenic_one, minutes)
        if found:
            achieved_minutes, _alpha, route = found
            within = abs(achieved_minutes - minutes) <= 0.2 * minutes
            label = (
                f"Scenic ~{int(achieved_minutes)} min"
                if within
                else "Scenic"
            )
            add(route, label)

    for alpha, label in (
        (3.0, "More scenic"),
        (8.0, "Most scenic"),
    ):
        add(scenic_one(alpha), label)

    return out[:max(k, 2)]


def plan_loop(graph: Graph, scorer, origin: int, minutes: float,
              alpha: float = 4.0, weather: float = 0.0,
              weather_factor: float = 1.0, k: int = 3):
    """Round trip of roughly `minutes`, back to where it started."""
    budget_s = minutes * 60.0
    half = budget_s / 2.0
    _d, ps, pn, el = graph.dijkstra(origin, scorer, alpha, weather=weather,
                                    weather_factor=weather_factor,
                                    max_seconds=half * 1.3)
    o = graph.node_pos(origin)
    ring = [(n, t) for n, t in el.items() if 0.6 * half <= t <= 1.2 * half and n != origin]
    if not ring:
        ring = [(n, t) for n, t in el.items() if t > half * 0.3]
    if not ring:
        return []

    # Spread candidates across bearings so the options genuinely differ instead
    # of being three variants of the same valley.
    buckets: dict[int, list] = defaultdict(list)
    for node, t in ring:
        buckets[int(bearing_deg(o, graph.node_pos(node)) // 45)].append((node, t))
    cands = []
    for items in buckets.values():
        items.sort(key=lambda x: -x[1])
        cands.extend(items[:2])

    routes = []
    for node, _t in cands[:14]:
        out_segs = graph.path_segments(ps, pn, origin, node)
        if not out_segs:
            continue
        _d2, ps2, pn2, _e2 = graph.dijkstra(node, scorer, alpha, goal=origin,
                                            weather=weather,
                                            weather_factor=weather_factor,
                                            avoid=set(out_segs))
        back = graph.path_segments(ps2, pn2, node, origin)
        if not back:
            continue
        r = build_route(graph, scorer, out_segs + back, origin)
        if r["kpis"]["km"] < 3:
            continue
        r["turnaround"] = {"lat": graph.node_pos(node)[0], "lon": graph.node_pos(node)[1]}
        r["bearing"] = round(bearing_deg(o, graph.node_pos(node)))
        r["overlap"] = round(len(set(out_segs) & set(back)) / max(len(out_segs), 1), 2)
        r["alpha"] = alpha
        # Rank on scenic per hour, then by how close it lands to the budget.
        r["score"] = r["kpis"]["scenic"] - 0.4 * abs(r["kpis"]["minutes"] - minutes) / minutes
        routes.append(r)

    routes.sort(key=lambda r: -r["score"])
    picked, used = [], set()
    for r in routes:
        b = r["bearing"] // 45
        if b in used:
            continue
        picked.append(r)
        used.add(b)
        if len(picked) >= k:
            break
    for i, r in enumerate(picked):
        r["label"] = ["Round trip", "Alternative", "Long way round"][i] if i < 3 else f"Option {i+1}"
    return picked or routes[:k]
