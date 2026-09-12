# The original algorithm, restored

`main`/`dev` route on **OSM road segments**. This branch restores the version
the project started with, where the **morton square is the routable unit and
the crowd data is the map**. Nothing here downloads a road network.

The spec is `plan/ALGORITHM_CELLS.md` — the `plan/ALGORITHM.md` as it stood at
commit `b38f8d4`, before `a1498a1` ("Score on road segments") replaced cells
with segments. HEAD's section 2 ("Why not a grid") is the rebuttal to it; this
branch is the thing being rebutted, running on real data.

## The one idea

Ordinary navigation asks *"which way is fastest?"* and adds up minutes. We run
**the same search — Dijkstra — but we lie to it about how long each road
takes**:

```
value(e) = w_fun·fun(e) + w_scenic·scenic(e) + w_growth·growth(e, rider)
risk(e)  = braking + hard decel + weather + congestion
time(e)  = L / v            # v from the crowd's own observed speed

cost(e)  = time(e) · ( 1 + α·(1 − value(e)) + β·risk(e) )

EXCLUDE e if crowd_lean_p95(e) − rider_lean_p95 > margin
```

A boring road is told it is longer than it looks; a great road is told it is
shorter. Then we just ask for the cheapest path, and what comes back is a good
*ride* rather than a fast *trip*.

Four properties carry the whole design:

1. **Every term is positive.** `time > 0` and the multiplier is `≥ 1`, so there
   are no negative-weight arcs and Dijkstra stays provably correct. No
   Bellman-Ford, no second engine.
2. **`α` is the single user-facing dial** — the Chill ↔ Sportive slider. It is
   literally *"how much extra time will you accept for a better road"*.
3. **Safety is a hard exclusion, not a penalty.** A soft penalty can always be
   overwhelmed by a large enough fun bonus; an exclusion cannot. Squares the
   crowd rides far beyond the rider's own lean p95 leave their graph entirely.
4. **Speed has a documented fallback chain** — square p85 → derived `d/t` →
   regional median. A missing `v` must never become a divide-by-zero shortcut.

## The map is the crowd

Nodes are level-18 morton squares (~100 m) that riders have actually ridden.
Edges are **observed square→square transitions**, carrying the speed riders
actually held. Two filters are not optional:

- **drop transitions whose raw GPS gap exceeds 150 m** — a 30 s dropout
  otherwise becomes a 2 km free shortcut that Dijkstra will find and love
  (BMW's own viewer guards this at `app.js:176`)
- **require ≥ 2 distinct trips per edge** — one stray sample must not create a
  road, and this is what prunes parallel-road collapse when a motorway and its
  frontage road land in the same square

## Files

| | |
|---|---|
| `precompute/morton.py` | the encoder, verified against the dataset's own `morton_code` column (Gate 0) |
| `precompute/build_cell_graph.py` | stages ① + ②: aggregate the squares, build the crowd graph, assert it |
| `engine/cell_router.py` | the cost distortion, Dijkstra, destination / time-budget / joyride modes |
| `scripts/demo_cell_router.py` | runs all of it and prints the working |

## Running it

```bash
python3 -m precompute.morton <any trip csv>     # Gate 0: encoder vs dataset
python3 precompute/build_cell_graph.py          # ~25 min -> data/cell_graph.json
python3 scripts/demo_cell_router.py             # the demo
```

`data/` is gitignored, so the graph has to be built before the demo runs. The
default corpus is the three example users plus 96 shards of each datalake half
— **32,450 trips, 8.8M samples**, giving 117,929 squares and 122,314
transitions in a 49 MB graph. Every number below and in the demo output was
measured on exactly that. Passing folder arguments builds a smaller graph and
will give different numbers, including a weaker α inversion.

## What it does and does not deliver

**Works.** The encoder is exact against the dataset's own `morton_code`. The
graph builds and passes both filters. And the doc's own *primary correctness
test* passes: between a fast dull road and a slow good one of the same length,
the preference inverts at **α ≈ 4.8** — the worked example predicted 3, and
the gap is honest: the crowd's value scores are shrunk toward the regional mean
by the confidence weight, so they span roughly 0.2–0.9 rather than the doc's
0.1–0.9, and it takes a little more α to cover the difference.

**Runs out.** End to end, α barely moves a long A→B route — Munich→Kesselberg
is the same ~91 km at α = 0, 1.5 and 3. Only **10% of squares offer any
onward choice at all**; the other 90% are chain links. There is usually one
corridor and nothing for the distortion to reorder. That is precisely the
argument HEAD's `plan/ALGORITHM.md` §2 makes for scoring road segments instead
of squares — measured here rather than asserted.

## What the data forced

Four places the spec was half right, all found by running it:

- `ridingabsbraking` reads 1 on essentially every row. It is an ABS-*available*
  flag, not an intervention count, and carries no risk signal — brake pressure
  does. Hard decel is logged in g, so the threshold is −0.25 g, not −3 m/s².
- `ridingvehiclespeed` is **dead for about a quarter of trips** — every row
  0.0. Both the graph's speeds and the rider's lean p95 fall back to GPS.
  Derived speeds are clamped to 200 km/h, so the fastest autobahn squares sit
  at the clamp rather than at a measured figure.
- The raw GPS gap at a square boundary is metres, not a length. It is the
  teleport guard; the distance ridden crossing a square is centre to centre.
  Using the gap under-reported Munich→Kesselberg as 20 km.
- Climb must come off a smoothed profile, or altitude noise over ~900 samples
  invents 13 km of ascent on a 90 km ride.
