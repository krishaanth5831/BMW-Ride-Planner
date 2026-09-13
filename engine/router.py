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

from precompute.build_osm_graph import haversine_m

# A junction only costs you if you stop or turn. Passing a side road on
# priority is free -- which is what stops "fewest junctions" from quietly
# becoming a motorway-seeking objective, since motorways have almost no
# at-grade junctions. Values are SECONDS, so they stay commensurate with travel
# time and cannot blow up into absurd detours.
# (*) Both are guesses standing in for a measurement we have not made. The
# designed version reads the delay straight off the crowd data -- observed speed
# drop across each junction per turn per time band (plan/ALGORITHM.md section 4,
# Fix 2). These are the documented tag-free fallback until that exists.
TURN_ATTENTION_S = 5.0     # (*)
JUNCTION_DEGREE_S = 3.0    # (*)
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
        self._components: dict[int, int] = {}
        component = 0
        for root in self.adj:
            if root in self._components:
                continue
            self._components[root] = component
            stack = [root]
            while stack:
                node = stack.pop()
                for sid in self.adj.get(node, ()):
                    other = self.other_end(self.by_id[sid], node)
                    if other not in self._components:
                        self._components[other] = component
                        stack.append(other)
            component += 1
        self._node_pos: dict[int, tuple[float, float]] = {}
        for s in segments:
            g = s["geometry"]
            self._node_pos.setdefault(s["node_a"], (g[0][0], g[0][1]))
            self._node_pos.setdefault(s["node_b"], (g[-1][0], g[-1][1]))
        self._annotate_road_runs()

    def _annotate_road_runs(self) -> None:
        """Give every segment the length of the whole road it belongs to.

        Segments are ~100 m chunks, so a single segment's own length says
        nothing about whether you are on a long uninterrupted run. The run is
        the OSM way (falling back to the road NAME when a way is split, which
        is how a single B-road arrives as a dozen way ids), so a 14 km
        dead-straight Bundesstrasse reads as 14 km of uninterrupted road even
        though it is 140 separate segments.
        """
        run_m: dict = defaultdict(float)
        keys: list = []
        for s in self.segments:
            key = ("name", s["name"], s.get("highway")) if s.get("name") \
                else ("way", s.get("way_id"))
            keys.append(key)
            run_m[key] += float(s.get("length_m") or 0.0)
        for s, key in zip(self.segments, keys):
            if s.get("road_run_km") is None:
                s["road_run_km"] = round(run_m[key] / 1000.0, 3)

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

    def nearest_node(self, lat: float, lon: float, require_degree: int = 1,
                     component: int | None = None):
        best, best_d = None, float("inf")
        for node, (nlat, nlon) in self._node_pos.items():
            if self.degree.get(node, 0) < require_degree:
                continue
            if component is not None and self._components.get(node) != component:
                continue
            d = (nlat - lat) ** 2 + ((nlon - lon) * math.cos(math.radians(lat))) ** 2
            if d < best_d:
                best, best_d = node, d
        return best

    def component_of(self, node: int) -> int | None:
        return self._components.get(node)

    # ------------------------------------------------------------------
    def dijkstra(self, start: int, scorer, alpha: float, *, goal=None,
             weather: float = 0.0, weather_factor: float = 1.0,
             max_seconds=None, avoid=None, blocked_highway_types=None,
             penalties=None, highway_avoidance: float = 0.0,
             twist_avoidance: float = 0.0, traffic_avoidance: float = 0.0):
        avoid = avoid or set()
        penalties = penalties or {}
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
                step = scorer.cost(seg, alpha, weather=weather, junction_s=jc,
                                   highway_avoidance=highway_avoidance,
                                   twist_avoidance=twist_avoidance,
                                   traffic_avoidance=traffic_avoidance)
                if seg_id in avoid:
                    step += scorer.seconds(seg) * 4.0
                # Soft penalty used to push each successive alternative onto
                # different roads without forbidding anything outright.
                pen = penalties.get(seg_id)
                if pen:
                    step += scorer.seconds(seg) * pen
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
    scenic_w = fun_w = personal_w = risk_w = conf_w = 0.0
    twist_w = traffic_w = twisty_m = 0.0
    max_run_km = long_straight_m = 0.0
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
        fun_w += scorer.fun(seg) * L
        personal_w += scorer.personal(seg) * L
        risk_w += scorer.risk(seg) * L
        conf_w += scorer.confidence(seg) * L
        twist = scorer.twist_score(seg)
        twist_w += twist * L
        traffic_w += scorer.traffic_pressure(seg) * L
        if twist >= 0.6:
            twisty_m += L
        run_km = float(seg.get("road_run_km") or L / 1000.0)
        max_run_km = max(max_run_km, run_km)
        # Metres spent on a long road that is NOT twisty -- the thing the
        # heatmap is trying to minimise, as opposed to a long alpine pass.
        if run_km >= 2.0 and twist < 0.4:
            long_straight_m += L
        if seg.get("n_trips"):
            covered_m += L
        if seg.get("dem_elev_m") is not None:
            elev.append(float(seg["dem_elev_m"]))
        elif seg.get("elev_mean"):
            elev.append(float(seg["elev_mean"]))
        if seg.get("name"):
            names.append(seg["name"])
        node = graph.other_end(seg, node)
        if graph.degree.get(node, 2) != 2:
            junctions += 1

    # Elevation gain has to be computed on a SMOOTHED profile. At 100 m chunks a
    # 76 km route is ~760 DEM samples, and summing every raw up-tick accumulates
    # metre-scale sampling noise into thousands of fictitious metres of climb.
    # Smooth over ~500 m, then ignore rises under 2 m.
    if len(elev) >= 5:
        win = 5
        sm = [sum(elev[max(0, i - win // 2):i + win // 2 + 1])
              / len(elev[max(0, i - win // 2):i + win // 2 + 1])
              for i in range(len(elev))]
    else:
        sm = elev
    elev_gain = sum(d for i in range(1, len(sm))
                    if (d := sm[i] - sm[i - 1]) > 2.0)
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
            "fun": round(fun_w / denom, 3),
            "personal": round(personal_w / denom, 3),
            "risk": round(risk_w / denom, 3),
            "confidence": round(conf_w / denom, 3),
            "crowd_covered_pct": round(100 * covered_m / denom, 0),
            "curviness": round(lean_delta / max(ridden_m / 1000.0, 1e-6), 1) if ridden_m else 0.0,
            "leaned_share": round(leaned_m / ridden_m, 3) if ridden_m else 0.0,
            "twist_score": round(twist_w / denom, 3),
            "twisty_pct": round(100 * twisty_m / denom, 0),
            "traffic_pressure": round(traffic_w / denom, 3),
            "max_road_run_km": round(max_run_km, 1),
            "long_straight_km": round(long_straight_m / 1000.0, 1),
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


def _route_overlap(graph: Graph, candidate: dict, other: dict) -> float:
    """Length-weighted overlap, stable even when chunks have unequal lengths."""
    shared = set(candidate["seg_ids"]) & set(other["seg_ids"])
    length = sum(float(graph.by_id[s]["length_m"]) for s in shared)
    total = sum(float(graph.by_id[s]["length_m"]) for s in candidate["seg_ids"])
    return length / total if total else 0.0


def plan_heatmap(graph: Graph, scorer, start: int, goal: int,
                 radius_km: float = 100, n: int = 30,
                 weather: float = 0.0, weather_factor: float = 1.0,
                 highway_avoidance: float = 0.0,
                 twist_avoidance: float = 0.0,
                 traffic_avoidance: float = 0.0) -> dict:
    """Sample distinct A→B routes and aggregate their scored road usage."""
    start_pos, goal_pos = graph.node_pos(start), graph.node_pos(goal)
    straight_km = haversine_m(start_pos, goal_pos) / 1000.0
    max_len_km = min(straight_km * 1.6, straight_km + max(0.0, radius_km))
    alpha_max = min(10.0, max(0.0, float(getattr(scorer, "alpha_max", 10.0))))
    alphas = [a for a in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0) if a <= alpha_max]
    if not alphas:
        alphas = [0.0]

    penalties = defaultdict(float)
    candidates: list[dict] = []
    for alpha in alphas:
        for _ in range(7):
            _d, ps, pn, _el = graph.dijkstra(
                start, scorer, alpha, goal=goal, weather=weather,
                weather_factor=weather_factor, penalties=penalties,
                highway_avoidance=highway_avoidance,
                twist_avoidance=twist_avoidance,
                traffic_avoidance=traffic_avoidance)
            segs = graph.path_segments(ps, pn, start, goal)
            if not segs:
                break
            route = build_route(graph, scorer, segs, start)
            if route["kpis"]["km"] <= max_len_km:
                if all(_route_overlap(graph, route, old) <= 0.55
                       for old in candidates):
                    route["alpha"] = alpha
                    candidates.append(route)
                    if len(candidates) >= n:
                        break
            for sid in segs:
                penalties[sid] += 0.8
        if len(candidates) >= n:
            break

    candidates.sort(key=lambda r: (-r["kpis"]["scenic"], -r["kpis"]["fun"]))
    candidates = candidates[:n]
    for rank, route in enumerate(candidates):
        route["rank"] = rank
        route["label"] = "Heatmap route" if rank == 0 else f"Alternative #{rank + 1}"

    raw_weights: dict[int, float] = defaultdict(float)
    for route in candidates:
        for sid in route["seg_ids"]:
            seg = graph.by_id[sid]
            raw_weights[sid] += scorer.scenic(seg) * float(seg["length_m"])
    max_weight = max(raw_weights.values(), default=0.0)
    heatmap = []
    for sid, value in raw_weights.items():
        seg = graph.by_id[sid]
        heatmap.append({
            "seg_id": sid,
            "geometry": [[p[0], p[1]] for p in seg["geometry"]],
            "scenic": round(scorer.scenic(seg), 3),
            "fun": round(scorer.fun(seg), 3),
            "personal": round(scorer.personal(seg), 3),
            "weight": round(value / max_weight, 3) if max_weight else 0.0,
        })
    heatmap.sort(key=lambda h: -h["weight"])
    return {
        "routes": candidates,
        "heatmap": heatmap,
        "meta": {"straight_km": round(straight_km, 1),
                 "max_route_km": round(max_len_km, 1),
                 "sampled": len(candidates),
                 "highway_avoidance": highway_avoidance,
                 "twist_avoidance": twist_avoidance,
                 "traffic_avoidance": traffic_avoidance},
    }


def plan_alternatives(graph: Graph, scorer, start: int, goal: int, *,
                      n: int = 6, alpha: float = 4.0, weather: float = 0.0,
                      weather_factor: float = 1.0, max_overlap: float = 0.55):
    """Several genuinely different A->B routes, ranked by scenic score.

    Iterative penalty method: route, then make the roads just used more
    expensive, and route again. Each pass is pushed onto different roads without
    forbidding anything, so the network stays connected and a route is always
    found.

    Candidates that share more than `max_overlap` of their length with a route
    already accepted are discarded -- otherwise you get six near-identical lines
    and a menu that lies about having choices.

    Returned best-scenic-first, so the caller can hand out rank 0, then 1, then
    2 on repeated asks.
    """
    from collections import defaultdict as _dd

    accepted: list[dict] = []
    penalties: dict[int, float] = _dd(float)
    seen: list[set[int]] = []

    for attempt in range(n * 3):
        _d, ps, pn, _el = graph.dijkstra(
            start, scorer, alpha, goal=goal, weather=weather,
            weather_factor=weather_factor, penalties=penalties)
        segs = graph.path_segments(ps, pn, start, goal)
        if not segs:
            break
        sset = set(segs)

        # Overlap measured by LENGTH, not segment count: 100 m chunks make a
        # raw count meaningless.
        def frac_shared(other: set[int]) -> float:
            shared = sum(graph.by_id[s]["length_m"] for s in sset & other)
            total = sum(graph.by_id[s]["length_m"] for s in sset)
            return shared / total if total else 0.0

        novel = all(frac_shared(o) <= max_overlap for o in seen)
        if novel:
            r = build_route(graph, scorer, segs, start)
            r["alpha"] = alpha
            accepted.append(r)
            seen.append(sset)
            if len(accepted) >= n:
                break
        # Penalise this path either way, so the next pass must diverge.
        bump = 0.8 if novel else 2.0
        for s in segs:
            penalties[s] += bump

    accepted.sort(key=lambda r: -r["kpis"]["scenic"])
    for i, r in enumerate(accepted):
        r["rank"] = i
        r["label"] = ("Most scenic" if i == 0
                      else f"#{i + 1} most scenic")
    return accepted
