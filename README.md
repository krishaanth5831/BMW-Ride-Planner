# BMW Ride Planner

A ride planner built for motorcyclists, not for commuters.

Most routing software answers *"what is the fastest way from A to B?"* That is
the wrong question for a rider on a Sunday morning. This answers a different
one: **what is the best ride I can have today?**

Built for the **BMW Motorrad track** at the TUM.ai Zurich hackathon.
Design docs live in [`plan/`](plan/) — start with
[`plan/ALGORITHM.md`](plan/ALGORITHM.md).

---

## What works today

Upload a rider's telemetry and get routes back. **No ride preferences are
asked for** — the start point, the ride length and the scoring weights are all
derived from the uploaded data:

| Derived | From |
|---|---|
| Where the ride starts | the start point of their own most frequent trips |
| How long it runs | the median duration of their recorded rides |
| What counts as "good" | revealed preference — which roads they chose, vs the crowd |
| What is too much | road-normalised style ratio → a hard capability ceiling |
| Bike class | rpm-per-km/h, gear count, lean envelope (inferred, confirmable) |

## Quick start

```bash
pip install duckdb fastapi uvicorn numpy pyarrow python-multipart

# 1. Build the road graph from the crowd data (~4 min over all 85,699 trips).
#    Add --shards 'trips-samples-1/0*' for a fast subset while developing.
python -m precompute.build_graph --shards 'trips-samples-*/*' --out data

# 2. Serve the API and UI together on one port.
python -m uvicorn engine.api:app --port 8000
```

Open <http://localhost:8000>, then either upload `.csv` files (or a `.zip` of
them) or click **A** / **B** / **C** to use a bundled example rider.

The dataset is expected at `~/Desktop/BMW/exd_download/datasetHackathon`.
Override with `BMW_DATASET=/path/to/datasetHackathon`.

## API keys

**None are needed.** The routing graph is built from BMW's own dataset and
Open-Meteo requires no key. See [`.env.example`](.env.example) — the only
optional key is live traffic (HERE or TomTom), and the default congestion model
is derived from the fleet's own observed speeds instead.

## Layout

```
precompute/   morton.py      encoder ported from BMW's tripViewer, verified
                             against the dataset's own morton_code column
              build_graph.py crowd trips -> segments + routable graph
engine/       profile.py     rider profile: taste / capability / context
              scoring.py     the four KPI scores and the cost function
              router.py      Dijkstra, junction costs, joyride loops
              weather.py     Open-Meteo, cached, degrades to neutral offline
              api.py         FastAPI + static UI
web/                         upload, map, routes, KPI panel
plan/                        design docs
```

## Verification

```bash
# Gate 0: the morton encoder must reproduce the dataset's own morton_code.
# A wrong encoder invalidates the whole precompute, so this runs first.
python -m precompute.morton \
  "$HOME/Desktop/BMW/exd_download/datasetHackathon/exampleUserA/recordedTrips/02cbe62f-d6a6-493a-a4e2-980e7536c3bd.csv"
```

## Known gaps

- **Segments come from the crowd data, not OSM.** This is the documented no-download
  fallback: node identity is a morton level-18 cell and chains of degree-2 nodes
  are collapsed into segments. It routes only where BMW riders have ridden.
  Wiring the Geofabrik extract adds real topology, `maxspeed`, `surface` and
  turn restrictions.
- **Junction costs use tag-free fallback priors**, not the measured
  speed-drop delays described in `plan/ALGORITHM.md` §4.
- **Turn-expanded graph not built yet** — U-turns are blocked by refusing to
  re-enter the segment just used, which is weaker than a proper turn expansion.
- **No DEM, land cover or accident data yet.** Elevation comes from the trips'
  own GPS altitude.
- **Destination (A→B) mode is not exposed** — the current UI plans joyride loops,
  since a loop needs no destination input.
