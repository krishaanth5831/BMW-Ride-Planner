"""Synthetic proof that heatmap avoidance changes which road wins.

A tiny five-node graph: node 0 is the start, node 9 the goal, and four
parallel corridors between them. Each corridor is a chain of 100 m segments so
`road_run_km` is built the same way it is on the real network -- from the whole
road, not from one chunk.

Run:  python -m tests.test_avoidance_routing
"""

from __future__ import annotations

from engine.router import Graph
from engine.scoring import Scorer

HEATMAP = dict(highway_avoidance=1.0, twist_avoidance=2.5, traffic_avoidance=2.0)


def chain(seg_id0, node0, mid_base, n, **fields):
    """n 100 m segments from node0 to node0+1, through fresh middle nodes."""
    out, prev = [], node0
    for i in range(n):
        nxt = node0 + 1 if i == n - 1 else mid_base + i
        lat = 48.0 + 0.001 * i
        out.append(dict(
            seg_id=seg_id0 + i, way_id=seg_id0, node_a=prev, node_b=nxt,
            length_m=100.0, geometry=[[lat, 11.0], [lat + 0.001, 11.0]],
            n_trips=20, tunnel=False, oneway=False, **fields))
        prev = nxt
    return out


def build():
    """Four corridors, 0 -> 1, each 20 segments (2 km of road)."""
    segs = []
    # A: motorway, dead straight, fast, moderate traffic.
    segs += chain(1000, 0, 10_000, 20, name="A8", highway="motorway",
                  speed_mean=120.0, speed_assumed=120, class_scenic=0.1,
                  curviness=0.2, curvature_geo=0.2, crawl_share=0.05,
                  band_share=0.3, leaned_share=0.02, lean_p95=8.0)
    # B: winding secondary, slower, quiet -- what heatmap mode should pick.
    segs += chain(2000, 0, 20_000, 20, name="Kesselbergstrasse",
                  highway="secondary", speed_mean=55.0, speed_assumed=60,
                  class_scenic=0.8, curviness=9.0, curvature_geo=9.0,
                  crawl_share=0.02, band_share=0.9, leaned_share=0.5,
                  lean_p95=30.0)
    # C: long straight secondary, same speed as B but no corners at all.
    segs += chain(3000, 0, 30_000, 20, name="B472 straight",
                  highway="secondary", speed_mean=55.0, speed_assumed=60,
                  class_scenic=0.5, curviness=0.1, curvature_geo=0.1,
                  crawl_share=0.02, band_share=0.9, leaned_share=0.02,
                  lean_p95=10.0)
    # D: clone of C's geometry and speed, but crawling with traffic.
    segs += chain(4000, 0, 40_000, 20, name="B11 jammed",
                  highway="secondary", speed_mean=55.0, speed_assumed=60,
                  class_scenic=0.5, curviness=0.1, curvature_geo=0.1,
                  crawl_share=0.85, band_share=0.1, leaned_share=0.02,
                  lean_p95=10.0)
    adj = {}
    for s in segs:
        adj.setdefault(s["node_a"], []).append(s["seg_id"])
        adj.setdefault(s["node_b"], []).append(s["seg_id"])
    return Graph(segs, adj), Scorer(segs, None)


def corridor(graph, seg_ids):
    return {1000: "motorway", 2000: "winding", 3000: "straight",
            4000: "jammed"}[graph.by_id[seg_ids[0]]["way_id"]]


def route(graph, scorer, **kw):
    _d, ps, pn, _e = graph.dijkstra(0, scorer, 0.0, goal=1, **kw)
    return graph.path_segments(ps, pn, 0, 1)


def check(name, got, want):
    print(f"  {'PASS' if got == want else 'FAIL'}  {name}: {got} (want {want})")
    return got == want


def main() -> int:
    graph, scorer = build()
    ok = []

    # road_run_km must describe the whole 2 km road, not one 100 m chunk.
    runs = {corridor(graph, [s["seg_id"]]): s["road_run_km"]
            for s in graph.segments}
    print("road_run_km per corridor:", runs)
    ok.append(check("road_run_km is the whole road", all(v == 2.0 for v in runs.values()), True))

    # 1. No avoidance -> the fast motorway wins on time.
    ok.append(check("1. avoidance off picks the motorway",
                    corridor(graph, route(graph, scorer)), "motorway"))

    # 2. Heatmap avoidance -> the slower winding secondary wins.
    ok.append(check("2. heatmap avoidance picks the winding road",
                    corridor(graph, route(graph, scorer, **HEATMAP)), "winding"))

    # 3. Long straight secondary loses to the twisty road of equal speed.
    straight = sum(scorer.cost(graph.by_id[i], 0.0, **HEATMAP) for i in range(3000, 3020))
    twisty = sum(scorer.cost(graph.by_id[i], 0.0, **HEATMAP) for i in range(2000, 2020))
    ok.append(check("3. long straight costs more than twisty", straight > twisty, True))

    # 4. High-crawl road loses to the identical low-traffic road.
    jammed = sum(scorer.cost(graph.by_id[i], 0.0, **HEATMAP) for i in range(4000, 4020))
    ok.append(check("4. jammed costs more than clear", jammed > straight, True))

    # 5. Every edge cost stays strictly positive, for every avoidance setting.
    costs = [scorer.cost(s, a, **kw)
             for s in graph.segments for a in (0.0, 4.0, 10.0)
             for kw in ({}, HEATMAP)]
    ok.append(check("5. all edge costs > 0", min(costs) > 0, True))

    print("all passed" if all(ok) else "FAILURES")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
