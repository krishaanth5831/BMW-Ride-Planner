"""Dijkstra over the segment graph, plus the joyride loop builder.

plan/ALGORITHM.md section 10. One search, used four times for the loop -- there
is no second engine.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict

from precompute.build_graph import node_center

# A junction only costs you if you stop or turn. Passing a side road on
# priority is free -- which is what stops "fewest junctions" from silently
# becoming a motorway-seeking objective. These are the tag-free fallback priors,
# in SECONDS, so they stay commensurate with travel time and cannot blow up into
# absurd detours (plan/ALGORITHM.md section 4, Fix 1 and Fix 2).
TURN_ATTENTION_S = 5.0      # any junction where the road you are on changes
JUNCTION_DEGREE_S = 3.0     # per extra branch: a proxy for control complexity


class Graph:
    def __init__(self, segments: list[dict], adjacency: dict):
        self.segments = segments
        self.by_id = {s["seg_id"]: s for s in segments}
        self.adj: dict[int, list[int]] = {int(k): v for k, v in adjacency.items()}
        self.degree = {n: len(v) for n, v in self.adj.items()}

    def other_end(self, seg: dict, node: int) -> int:
        return seg["node_b"] if node == seg["node_a"] else seg["node_a"]

    def junction_cost(self, node: int, turning: bool) -> float:
        """Measured delay would go here; this is the documented fallback prior."""
        if not turning:
            return 0.0
        extra = max(0, self.degree.get(node, 2) - 2)
        return TURN_ATTENTION_S + JUNCTION_DEGREE_S * extra

    def nearest_node(self, lat: float, lon: float) -> int | None:
        best, best_d = None, float("inf")
        for node in self.adj:
            nlat, nlon = node_center(node)
            d = (nlat - lat) ** 2 + ((nlon - lon) * math.cos(math.radians(lat))) ** 2
            if d < best_d:
                best, best_d = node, d
        return best

    # ------------------------------------------------------------------
    def dijkstra(self, start: int, scorer, alpha: float, *, goal: int | None = None,
                 w_growth: float = 0.0, weather: float = 0.0,
                 max_seconds: float | None = None,
                 avoid: set[int] | None = None, weather_factor: float = 1.0):
        """Cheapest-first expansion. Returns (dist, prev_seg, prev_node, elapsed).

        `dist` is in *cost* units; `elapsed` tracks real seconds so a time
        budget can be read off directly.
        """
        avoid = avoid or set()
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
                seg = self.by_id[seg_id]
                if seg_id == via:
                    continue  # no immediate U-turn back down the same segment
                if scorer.excluded(seg, weather_factor):
                    continue
                nxt = self.other_end(seg, node)
                if nxt == node:
                    continue
                pen = 1.0 if seg_id in avoid else 0.0
                turning = via != -1
                jc = self.junction_cost(node, turning)
                step = scorer.cost(seg, alpha, w_growth=w_growth, weather=weather,
                                   junction_s=jc)
                step += pen * scorer.seconds(seg) * 4.0  # reuse penalty
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

    def path_segments(self, prev_seg, prev_node, start: int, end: int) -> list[int]:
        out: list[int] = []
        cur = end
        guard = 0
        while cur != start and cur in prev_seg and guard < 100_000:
            out.append(prev_seg[cur])
            cur = prev_node[cur]
            guard += 1
        out.reverse()
        return out


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def build_route(graph: Graph, scorer, seg_ids: list[int], origin: int) -> dict:
    """Assemble a Track: ordered geometry, KPIs, and the explanation payload."""
    coords: list[list[float]] = []
    node = origin
    total_m = total_s = 0.0
    ridden_m = 0.0          # distance summed over traversals, for invariant ratios
    lean_delta = leaned_m = abs_events = 0.0
    elev: list[float] = []
    speeds: list[float] = []
    per_seg = []
    junctions = 0     # real junctions passed, i.e. nodes of degree != 2
    for sid in seg_ids:
        seg = graph.by_id[sid]
        geom = seg["geometry"]
        # Orient each polyline so the route reads start-to-finish.
        if node == seg["node_b"]:
            geom = list(reversed(geom))
        if coords and coords[-1] == [geom[0][0], geom[0][1]]:
            geom = geom[1:]
        coords.extend([[p[0], p[1]] for p in geom])
        total_m += seg["length_m"]
        ridden_m += seg["path_m"] or 0.0
        total_s += scorer.seconds(seg)
        lean_delta += seg["lean_delta"] or 0
        leaned_m += seg["leaned_m"] or 0
        abs_events += seg["abs_events"] or 0
        if seg.get("elev_mean"):
            elev.append(seg["elev_mean"])
        if seg.get("speed_mean"):
            speeds.append(seg["speed_mean"])
        k = scorer.kpis(seg)
        per_seg.append({"seg_id": sid, **k})
        node = graph.other_end(seg, node)
        # Only count a junction where the rider actually has a choice to make.
        # Counting segment boundaries instead would inflate this badly, since a
        # long road is many segments with no junction between them.
        if graph.degree.get(node, 2) != 2:
            junctions += 1

    km = max(total_m / 1000.0, 1e-6)
    elev_gain = 0.0
    for i in range(1, len(elev)):
        if elev[i] > elev[i - 1]:
            elev_gain += elev[i] - elev[i - 1]

    n = max(len(per_seg), 1)
    return {
        "coords": coords,
        "seg_ids": seg_ids,
        "kpis": {
            "km": round(km, 1),
            "minutes": round(total_s / 60.0, 1),
            # trip-count invariant: both sides summed over the same traversals
            "curviness": round(lean_delta / max(ridden_m / 1000.0, 1e-6), 1),
            "leaned_share": round(leaned_m / ridden_m, 3) if ridden_m else 0.0,
            "elev_gain_m": round(elev_gain, 0),
            "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else 0.0,
            "abs_per_100km": round(abs_events / max(ridden_m / 100_000.0, 1e-6), 1),
            "junctions": junctions,
            "fun": round(sum(p["fun"] for p in per_seg) / n, 3),
            "scenic": round(sum(p["scenic"] for p in per_seg) / n, 3),
            "risk": round(sum(p["risk"] for p in per_seg) / n, 3),
            "growth": round(sum(p["growth"] for p in per_seg) / n, 3),
            "confidence": round(sum(p["confidence"] for p in per_seg) / n, 3),
        },
        "per_segment": per_seg,
    }


def joyride(graph: Graph, scorer, origin_node: int, minutes: float,
            alpha: float = 3.0, weather: float = 0.0, weather_factor: float = 1.0,
            k_options: int = 3) -> list[dict]:
    """X hours from here, back to here. Four calls to the same Dijkstra.

    1. flood out from the origin -> travel time everywhere
    2. take the ring at about half the budget
    3. pick turnarounds spread across bearings, so the options genuinely differ
    4. route out, then route back with a reuse penalty (BMW's own
       `alreadyUsedRoads`), and rank the completed loops
    """
    budget_s = minutes * 60.0
    half = budget_s / 2.0
    dist, prev_seg, prev_node, elapsed = graph.dijkstra(
        origin_node, scorer, alpha, weather=weather, max_seconds=half * 1.25,
        weather_factor=weather_factor)

    o = node_center(origin_node)
    ring = [
        (n, t) for n, t in elapsed.items()
        if 0.55 * half <= t <= 1.15 * half and n != origin_node
    ]
    if not ring:
        ring = [(n, t) for n, t in elapsed.items() if t > 0]
    if not ring:
        return []

    # Bucket candidates by bearing so the three loops point different ways
    # rather than being three variants of the same valley.
    buckets: dict[int, list] = defaultdict(list)
    for node, t in ring:
        b = int(bearing(o, node_center(node)) // 45)
        buckets[b].append((node, t))

    cands = []
    for b, items in buckets.items():
        # best = furthest-value-per-time in that bearing
        items.sort(key=lambda x: -x[1])
        cands.append(items[0])
    cands.sort(key=lambda x: -x[1])

    routes = []
    for node, _t in cands[: max(k_options * 2, 6)]:
        out_segs = graph.path_segments(prev_seg, prev_node, origin_node, node)
        if not out_segs:
            continue
        # Return leg penalises roads already used on the way out.
        d2, ps2, pn2, el2 = graph.dijkstra(
            node, scorer, alpha, goal=origin_node, weather=weather,
            avoid=set(out_segs), weather_factor=weather_factor)
        back_segs = graph.path_segments(ps2, pn2, node, origin_node)
        if not back_segs:
            continue
        route = build_route(graph, scorer, out_segs + back_segs, origin_node)
        if route["kpis"]["km"] < 5:
            continue
        overlap = len(set(out_segs) & set(back_segs)) / max(len(out_segs), 1)
        route["turnaround"] = {"lat": node_center(node)[0], "lon": node_center(node)[1]}
        route["bearing"] = round(bearing(o, node_center(node)))
        route["overlap"] = round(overlap, 2)
        route["score"] = (
            route["kpis"]["fun"] * 0.5 + route["kpis"]["scenic"] * 0.5
        ) / max(route["kpis"]["minutes"] / 60.0, 0.25)
        routes.append(route)

    routes.sort(key=lambda r: -r["score"])
    # Keep bearing diversity in the final three.
    picked, used = [], set()
    for r in routes:
        b = r["bearing"] // 45
        if b in used and len(picked) < k_options:
            continue
        picked.append(r)
        used.add(b)
        if len(picked) >= k_options:
            break
    return picked or routes[:k_options]
