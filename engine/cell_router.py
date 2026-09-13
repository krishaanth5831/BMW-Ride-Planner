"""The ORIGINAL algorithm: cost-distorted Dijkstra over the morton cell graph.

plan/ALGORITHM.md at b38f8d4, sections 6-9. Restored on this branch because
HEAD routes on OSM road segments instead; this module is the version where the
morton square IS the routable unit and the crowd IS the map.

The one idea, from section 1: ordinary navigation asks "which way is fastest?"
and adds up minutes. We run the SAME search -- Dijkstra -- but we lie to it
about how long each road takes, in a controlled and explainable way:

    cost(e,t) = time(e,t) * ( 1 + alpha*(1 - value(e,t)) + beta*risk(e,t) )

A boring road is told it is longer than it looks; a great road is told it is
shorter. Then we just ask for the cheapest path, and what comes back is a good
*ride* rather than a fast *trip*.

The distortion never makes an edge free or negative: `time > 0` and the
multiplier is >= 1, so every arc stays strictly positive and Dijkstra remains
provably correct. That is the whole trick -- no Bellman-Ford, no second engine.
"""

from __future__ import annotations

import heapq
import json
import math
from collections import defaultdict

from precompute.morton import LEVEL_COARSE, cell_center

# Section 8: alpha is the single user-facing dial, the Chill <-> Sportive
# slider. It is literally "how much extra time will you accept for a better
# road". beta is the fixed safety weight.
ALPHA_CHILL, ALPHA_BALANCED, ALPHA_SPORTIVE = 0.0, 1.5, 3.0
# (*) Risk weighting in the cost function. Our choice.
BETA = 1.0              # (*)

# Section 8, property 3: safety is a HARD EXCLUSION, not a penalty. A soft
# penalty can always be overwhelmed by a large enough fun bonus; an exclusion
# cannot.
# (*) Headroom allowed above a rider's demonstrated lean. Nothing in the data
# says how much is safe -- this is a conservative guess.
LEAN_MARGIN_DEG = 8.0   # (*)


class CellGraph:
    """Nodes are ridden squares. Edges are observed square->square transitions."""

    def __init__(self, blob: dict):
        self.level = blob["level"]
        self.cells: dict[str, dict] = blob["cells"]
        self.region_speed = blob.get("region_speed_kmh", 40.0)
        # Edges and adjacency are built on FIRST USE, not here. The product
        # path (/ride) needs this class only for the crowd scores and their
        # norms: it routes on the OSM road graph and never walks these edges.
        # Building them anyway cost every request an adjacency of 244,628
        # mirrored dicts, about 67 MB, that nothing on that path reads.
        self._raw_edges = blob["edges"]
        self._edges = None
        self._adj = None
        self._norm = self._feature_norms()
        self.p95_brake = self._p95("abs_rate")
        self.p95_decel = self._p95("decel_rate")

    def _build_edges(self) -> None:
        """Transitions are observed directionally, but riding a road the other
        way is the same road. Collapse to one undirected edge per pair --
        mirroring without collapsing double-counts every road the crowd rode
        both ways, which quietly doubles the apparent branching of the graph.
        """
        merged: dict[tuple, dict] = {}
        for e in self._raw_edges:
            key = (e["a"], e["b"]) if e["a"] < e["b"] else (e["b"], e["a"])
            cur = merged.get(key)
            if cur is None or e["n_trips"] > cur["n_trips"]:
                merged[key] = e
        self._edges = list(merged.values())
        self._adj = defaultdict(list)
        for e in self._edges:
            self._adj[e["a"]].append(e)
            self._adj[e["b"]].append({**e, "a": e["b"], "b": e["a"]})

    @property
    def edges(self) -> list:
        if self._edges is None:
            self._build_edges()
        return self._edges

    @property
    def adj(self) -> dict:
        if self._adj is None:
            self._build_edges()
        return self._adj

    def branching(self) -> float:
        """Share of squares with a genuine choice of onward road."""
        n = sum(1 for v in self.adj.values() if len(v) >= 3)
        return n / max(len(self.adj), 1)

    def _p95(self, key: str) -> float:
        vals = sorted(float(c.get(key) or 0.0) for c in self.cells.values())
        if not vals:
            return 1.0
        return max(vals[int(0.95 * (len(vals) - 1))], 1e-3)

    # -- features ----------------------------------------------------
    def _feature_norms(self) -> dict:
        """Crowd baseline: mean and sd per feature, for the z-scores in s.5."""
        keys = ("lean_p50", "speed_med", "elev_m", "abs_rate", "decel_rate", "throttle")
        acc = {k: [] for k in keys}
        for c in self.cells.values():
            for k in keys:
                v = c.get(k)
                if v is not None:          # a square with no DEM fix is not a 0 m square
                    acc[k].append(float(v))
        out = {}
        for k, vals in acc.items():
            mu = sum(vals) / max(len(vals), 1)
            var = sum((v - mu) ** 2 for v in vals) / max(len(vals) - 1, 1)
            out[k] = (mu, math.sqrt(var) or 1.0)
        return out

    def z(self, cell: dict, key: str) -> float:
        v = cell.get(key)
        if v is None:            # unknown reads as the crowd average, not as zero
            return 0.0
        mu, sd = self._norm[key]
        return (float(v) - mu) / sd

    def seconds(self, edge: dict) -> float:
        """time(e,t) = L / v, with the documented speed fallback chain."""
        v = float(edge.get("v_kmh") or 0.0) or self.region_speed
        return (float(edge["len_m"]) / 1000.0) / max(v, 3.0) * 3600.0

    def nearest(self, lat: float, lon: float) -> str | None:
        """Snap to the nearest square that has data (section 9)."""
        best, best_d = None, float("inf")
        k = math.cos(math.radians(lat))
        for code, c in self.cells.items():
            d = (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * k) ** 2
            if d < best_d:
                best, best_d = code, d
        return best


class Rider:
    """Section 5: revealed preference, learned from the rider's own bike.

    Weights come from how far each feature of the squares THIS rider has
    ridden sits above the crowd baseline. No questionnaire.
    """

    def __init__(self, graph: CellGraph, ridden: list[str] | None = None,
                 lean_p95: float = 90.0):
        self.graph = graph
        self.ridden = set(ridden or [])
        self.lean_p95 = lean_p95
        self.visited_coarse = {c[:LEVEL_COARSE] for c in self.ridden}
        self.w = self._weights()

    def _weights(self) -> dict:
        keys = ("lean_p50", "elev_m", "throttle")
        if not self.ridden:
            return {k: 1.0 / len(keys) for k in keys}
        raw = {}
        for k in keys:
            zs = [self.graph.z(self.graph.cells[c], k)
                  for c in self.ridden if c in self.graph.cells]
            raw[k] = max(0.0, sum(zs) / max(len(zs), 1))
        total = sum(raw.values())
        return {k: (v / total if total else 1.0 / len(keys)) for k, v in raw.items()}


def _squash(z: float) -> float:
    """z-score -> 0..1, so every score term is bounded and the cost stays sane."""
    return 1.0 / (1.0 + math.exp(-z))


class Cost:
    """Section 8. Every term positive; the multiplier is never below 1."""

    def __init__(self, graph: CellGraph, rider: Rider, weather: float = 0.0):
        self.g = graph
        self.r = rider
        self.weather = weather

    # value ----------------------------------------------------------
    def fun(self, cell: dict) -> float:
        """Per-KILOMETRE, never BMW's own funFactor.

        The sample viewer's experimental formula (app.js:203) multiplies by
        pathKm, so a square scores higher simply for containing more road --
        an exposure count hiding inside a quality score. We normalise instead
        and use n_trips only as a confidence weight.
        """
        return _squash(0.7 * self.g.z(cell, "lean_p50") + 0.3 * self.g.z(cell, "throttle"))

    def scenic(self, cell: dict) -> float:
        return _squash(0.6 * self.g.z(cell, "elev_m") - 0.4 * self.g.z(cell, "speed_med"))

    def confidence(self, cell: dict) -> float:
        """Few riders -> shrink toward the regional mean (section 7)."""
        n = float(cell.get("n_trips") or 0)
        return n / (n + 3.0)

    def value(self, code: str, cell: dict) -> float:
        w = self.r.w
        new_terrain = 0.0 if code[:LEVEL_COARSE] in self.r.visited_coarse else 1.0
        lean = float(cell.get("lean_p50") or 0.0)
        stretch = 1.0 if self.r.lean_p95 * 0.85 <= lean <= self.r.lean_p95 else 0.0
        growth = 0.5 * stretch + 0.5 * new_terrain
        v = (w.get("lean_p50", 0.34) * self.fun(cell)
             + w.get("elev_m", 0.33) * self.scenic(cell)
             + w.get("throttle", 0.33) * growth)
        # Shrink an under-observed square toward the neutral 0.5.
        c = self.confidence(cell)
        return max(0.0, min(1.0, c * v + (1 - c) * 0.5))

    # risk -----------------------------------------------------------
    def risk(self, cell: dict) -> float:
        """Braking rate, hard decel, weather and congestion -- all in 0..1.

        Each rate is scaled against the crowd's own p95 rather than a made-up
        constant, so `risk` spreads across its range instead of saturating at
        1.0 and making every road look equally dangerous.
        """
        congestion = max(0.0, 1.0 - float(cell.get("speed_med") or 0.0)
                         / max(float(cell.get("speed_p85") or 0.0), 1e-6))
        brake = min(1.0, float(cell.get("abs_rate") or 0.0) / self.g.p95_brake)
        decel = min(1.0, float(cell.get("decel_rate") or 0.0) / self.g.p95_decel)
        return max(0.0, min(1.0, 0.4 * brake + 0.3 * decel
                            + 0.2 * self.weather + 0.1 * congestion))

    def excluded(self, cell: dict) -> bool:
        """Hard safety gate. Not a penalty -- the square leaves the graph."""
        return float(cell.get("lean_p95") or 0.0) - self.r.lean_p95 > LEAN_MARGIN_DEG

    # the distortion -------------------------------------------------
    def edge_cost(self, edge: dict, alpha: float) -> float:
        cell = self.g.cells[edge["b"]]
        t = self.g.seconds(edge)
        return t * (1.0 + alpha * (1.0 - self.value(edge["b"], cell))
                    + BETA * self.risk(cell))


def dijkstra(g: CellGraph, cost: Cost, start: str, alpha: float, *,
             goal: str | None = None, max_seconds: float | None = None,
             reuse: set | None = None):
    """Section 9, verbatim in shape. A binary heap and nothing exotic."""
    reuse = reuse or set()
    dist = {start: 0.0}
    secs = {start: 0.0}
    prev: dict[str, str] = {}
    heap = [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, float("inf")):
            continue                      # stale entry
        if goal is not None and u == goal:
            break
        for e in g.adj.get(u, ()):
            v = e["b"]
            cell = g.cells.get(v)
            if cell is None or cost.excluded(cell):
                continue                  # hard safety exclusion
            step = cost.edge_cost(e, alpha)
            if (u, v) in reuse or (v, u) in reuse:
                step *= 4.0               # BMW's own alreadyUsedRoads
            ns = secs[u] + g.seconds(e)
            if max_seconds is not None and ns > max_seconds:
                continue
            nd = d + step
            if nd < dist.get(v, float("inf")):
                dist[v], secs[v], prev[v] = nd, ns, u
                heapq.heappush(heap, (nd, v))
    return dist, secs, prev


def reconstruct(prev: dict, start: str, goal: str) -> list[str]:
    out, cur, guard = [goal], goal, 0
    while cur != start and cur in prev and guard < 1_000_000:
        cur = prev[cur]
        out.append(cur)
        guard += 1
    if out[-1] != start:
        return []
    out.reverse()
    return out


def summarise(g: CellGraph, cost: Cost, path: list[str]) -> dict:
    """KPIs over a path of squares, so we can show our working (section 10)."""
    if len(path) < 2:
        return {}
    index = {(e["a"], e["b"]): e for e in g.edges}
    total_m = total_s = 0.0
    val = rsk = lean = 0.0
    elev = []
    for a, b in zip(path, path[1:]):
        e = index.get((a, b)) or index.get((b, a))
        if e is None:
            continue
        L = float(e["len_m"])
        c = g.cells[b]
        total_m += L
        total_s += g.seconds(e)
        val += cost.value(b, c) * L
        rsk += cost.risk(c) * L
        lean += float(c.get("lean_p50") or 0.0) * L
        if c.get("elev_m") is not None:
            elev.append(float(c["elev_m"]))
    den = max(total_m, 1e-6)
    # Climb has to come off a SMOOTHED profile. At ~100 m squares a 90 km ride
    # is ~900 samples, and summing every raw up-tick turns metre-scale GPS
    # altitude noise into thousands of fictitious metres of ascent.
    win = 9
    sm = [sum(elev[max(0, i - win // 2):i + win // 2 + 1])
          / len(elev[max(0, i - win // 2):i + win // 2 + 1])
          for i in range(len(elev))] if len(elev) >= win else elev
    gain = sum(d for i in range(1, len(sm)) if (d := sm[i] - sm[i - 1]) > 1.0)
    return {
        "km": round(total_m / 1000.0, 1),
        "minutes": round(total_s / 60.0, 1),
        "squares": len(path),
        "value": round(val / den, 3),
        "risk": round(rsk / den, 3),
        "mean_lean_deg": round(lean / den, 1),
        "elev_gain_m": round(gain, 0),
    }


def plan_destination(g: CellGraph, cost: Cost, start: str, goal: str,
                     alphas=(ALPHA_CHILL, ALPHA_BALANCED, ALPHA_SPORTIVE)) -> list[dict]:
    """Mode 1. Three genuinely different routes, one per alpha (section 9)."""
    out = []
    for label, alpha in zip(("Chill", "Balanced", "Full Send"), alphas):
        _d, _s, prev = dijkstra(g, cost, start, alpha, goal=goal)
        path = reconstruct(prev, start, goal)
        if not path:
            continue
        out.append({"label": label, "alpha": alpha, "path": path,
                    "coords": [[g.cells[c]["lat"], g.cells[c]["lon"]] for c in path],
                    "kpis": summarise(g, cost, path)})
    return out


def plan_for_budget(g: CellGraph, cost: Cost, start: str, goal: str,
                    minutes: float, alpha_max: float = 8.0, iters: int = 7):
    """Section 9: a time budget is a binary search on alpha.

    Higher alpha means a longer, better route. Duration STEPS rather than
    curving smoothly because paths are discrete, so monotonicity is a smoke
    test only -- the search still converges.
    """
    lo, hi, best = 0.0, alpha_max, None
    for _ in range(iters):
        mid = (lo + hi) / 2
        _d, _s, prev = dijkstra(g, cost, start, mid, goal=goal)
        path = reconstruct(prev, start, goal)
        if not path:
            hi = mid
            continue
        k = summarise(g, cost, path)
        if best is None or abs(k["minutes"] - minutes) < abs(best[1]["minutes"] - minutes):
            best = (mid, k, path)
        if k["minutes"] < minutes:
            lo = mid
        else:
            hi = mid
    if best is None:
        return None
    alpha, kpis, path = best
    return {"alpha": round(alpha, 2), "kpis": kpis, "path": path,
            "coords": [[g.cells[c]["lat"], g.cells[c]["lon"]] for c in path]}


def bearing(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def joyride(g: CellGraph, cost: Cost, start: str, minutes: float,
            alpha: float = ALPHA_SPORTIVE, k: int = 3) -> list[dict]:
    """Mode 2: X hours from here, back to here. The same Dijkstra, four times.

    1. flood outward -> travel time to every reachable square
    2. take the ring at about T/2
    3. pick turnarounds spread across bearings, so the offered loops genuinely
       differ instead of being three views of the same valley
    4. route out, then back with a reuse penalty (BMW's alreadyUsedRoads)
    5. rank complete loops by value per hour
    """
    budget = minutes * 60.0
    half = budget / 2.0
    _d, secs, prev = dijkstra(g, cost, start, alpha, max_seconds=half * 1.25)
    index = {(e["a"], e["b"]): e for e in g.edges}

    origin = (g.cells[start]["lat"], g.cells[start]["lon"])
    # Turn around a little SHORT of half the budget: the way home avoids the
    # roads just used, so it is always the longer of the two legs. Sitting the
    # ring at exactly half guarantees an overrun.
    ring = [(c, t) for c, t in secs.items() if 0.40 * half <= t <= 0.85 * half]
    if not ring:
        return []

    buckets: dict[int, list] = defaultdict(list)
    for code, t in ring:
        b = int(bearing(origin, (g.cells[code]["lat"], g.cells[code]["lon"])) // 45)
        buckets[b].append((code, t))

    loops = []
    for b, items in buckets.items():
        items.sort(key=lambda x: -x[1])
        turn = items[0][0]
        out_path = reconstruct(prev, start, turn)
        if len(out_path) < 2:
            continue
        used = set(zip(out_path, out_path[1:]))
        # The way home has to fit what is LEFT of the budget. Leaving the
        # return leg unbounded is how "90 minutes from here" quietly becomes a
        # two-hour ride.
        out_s = sum(g.seconds(index[e] if e in index else index[(e[1], e[0])])
                    for e in used if e in index or (e[1], e[0]) in index)
        _d2, _s2, prev2 = dijkstra(g, cost, turn, alpha, goal=start, reuse=used,
                                   max_seconds=max(budget - out_s, 60.0) * 1.25)
        back = reconstruct(prev2, turn, start)
        if len(back) < 2:
            continue
        path = out_path + back[1:]
        kpis = summarise(g, cost, path)
        if kpis.get("km", 0) < 5 or kpis["minutes"] > minutes * 1.15:
            continue
        overlap = len(used & set(zip(back, back[1:]))) / max(len(used), 1)
        loops.append({
            "bearing": round(bearing(origin, (g.cells[turn]["lat"], g.cells[turn]["lon"]))),
            "turnaround": [g.cells[turn]["lat"], g.cells[turn]["lon"]],
            "overlap": round(overlap, 2),
            "path": path,
            "coords": [[g.cells[c]["lat"], g.cells[c]["lon"]] for c in path],
            "kpis": kpis,
            "score": kpis["value"] / max(kpis["minutes"] / 60.0, 0.25),
        })
    loops.sort(key=lambda r: -r["score"])
    for i, r in enumerate(loops[:k]):
        r["label"] = ["Round trip", "Alternative", "Long way round"][i]
    return loops[:k]


def load(path: str = "data/cell_graph.json") -> CellGraph:
    with open(path) as fh:
        return CellGraph(json.load(fh))
