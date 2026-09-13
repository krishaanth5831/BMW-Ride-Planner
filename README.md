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

Upload a rider's telemetry (a whole folder, individual files, or a zip) and get
routes back, on either **round trip** or **point to point**.

Legacy round-trip and point-to-point planning remains scenic-first. X→Y heatmap mode adds percentile-scaled fun and a profile-personal score while retaining capability as a hard exclusion.
The rider profile is still used, but only for things that are not preferences:
the capability ceiling (a hard exclusion), the start point, and the default ride
length.

Everything else is derived from the uploaded data:

| Derived | From |
|---|---|
| Where the ride starts | the start point of their own most frequent trips |
| How long it runs | the median duration of their recorded rides |
| What counts as "good" | revealed preference — which roads they chose, vs the crowd |
| What is too much | road-normalised style ratio → a hard capability ceiling |
| Bike class | rpm-per-km/h, gear count, lean envelope (inferred, confirmable) |

## X→Y heatmap

Choose **X → Y heatmap** in the UI, click A and B on the map, select a 30/50/100 km detour radius, and build the heatmap. The API is also available at `POST /api/heatmap` (or `/api/plan/heatmap`) with the same telemetry FormData as `/api/plan/upload` plus required `origin_lat/lon` and `dest_lat/lon`. It samples up to 30 distinct routes, scores each for scenic, fun, and personal fit, and returns a segment heatmap where greener lines score higher and thicker lines are used by more candidate routes. Coordinates outside the stored Bavaria bbox return `422` with the bbox in the error metadata.

Heatmap mode looks for the same thing a rider does — a road that keeps turning, with nothing in the way — through three positive cost multipliers. They are multipliers on travel time, so every edge cost stays strictly positive and Dijkstra stays valid.

* **Highways are avoided, not forbidden.** Motorways cost 5×, motorway links 4×, trunks 3×, trunk links 2.5×, primary roads 1× and primary links 0.8×. Nothing is hard-blocked, so when a motorway is the only way to connect X to Y the planner still uses it.
* **Long uninterrupted roads are penalised.** Each segment carries `road_run_km`, the length of the whole road it belongs to (grouped by OSM way, falling back to the road name), not the length of its own ~100 m chunk. Runs beyond 0.6 km are charged an increasing penalty — but only in proportion to how *un*-twisty they are, so a 14 km alpine pass full of corners is untouched while a 14 km dead-straight Bundesstrasse is expensive.
* **Repeated twists are preferred.** The twist score blends the crowd's measured lean rhythm with 15 m-resampled road geometry, weighted by how many trips actually cover the segment, so roads nobody has ridden are still judged on their real shape.
* **Traffic pressure comes from BMW crowd telemetry**, not a live traffic API — observed `crawl_share` and the inverse of `band_share`, blended by trip-count confidence against a road-class prior for roads the crowd has not covered. High traffic raises a road's cost; it never excludes it. No API keys and no extra dependencies.
* **Capability remains a hard exclusion.** A road demanding more lean than the rider's own envelope allows is removed from the graph outright, and no scenic, twist or traffic bonus can buy it back.

Heatmap routing applies `highway_avoidance=1.0`, `twist_avoidance=2.5` and `traffic_avoidance=2.0`; legacy loop and point-to-point routing leave all three at `0.0` and are unchanged. Each route reports `twist_score`, `twisty_pct`, `traffic_pressure`, `max_road_run_km` and `long_straight_km` alongside the existing scenic, fun, personal, crowd-coverage, junction and elevation KPIs.

## Where the numbers come from

Anything marked **(\*)** in the code is a value **we chose ourselves** — not from
the BMW dataset, an API, or the brief. Unmarked values are traceable to a named
source.

> The inputs are measured. The scoring is our opinion.

Lean angle, curviness, observed speed, ABS events, terrain and weather are all
real measurements. What makes a road *scenic* is a definition we wrote, and
every weight lives in one file you can argue with. Full inventory:
[plan/PROVENANCE.md](plan/PROVENANCE.md).

Two things not to overclaim: **junction cost** and **traffic** are currently
constants standing in for crowd-data measurements that are designed but not yet
built. Both are marked (\*).

## Quick start

```bash
# macOS, Linux, and Windows: no dataset, pip install, or precomputation needed.
python3 -m engine.demo_server
```

Open <http://localhost:8000/ride>. The lightweight mode routes on the committed
OSM fixture, uses clearly labeled sample rider values, and fetches live weather.
If a valid
`data/cell_graph.json` and BMW example-rider folders are present, the same app
automatically switches to telemetry-backed mode.

Weather windows come from the live Open-Meteo forecast API (no key required)
and are cached locally. If the service or network is unavailable, the UI falls
back to clearly marked bundled sample weather so the demo remains usable.
Each day is evaluated only from three hours before local sunset until one hour
after. A ride is confirmed when every hourly weather score passes and no hour
exceeds 30°C.

Scenic routes use major roads to leave Munich efficiently, then ramp in a
strong motorway/trunk penalty from 20–25 km away from Marienplatz. The roads
remain available when they are the only practical connection.
Long, locally straight named roads receive an additional geometry-based cost
outside the same radius; short connectors and genuinely curving roads do not.

### Full telemetry-backed setup

```bash
python3 -m venv .venv
source .venv/bin/activate                 # macOS / Linux
python -m pip install -r requirements.txt
python -m pip install duckdb

# 1. Fetch the road network from Overpass (~5 min, cached to fixtures/osm/).
python -m precompute.fetch_osm

# 2. Split ways at junctions and snap the crowd telemetry onto them (~5 min).
python -m precompute.build_osm_graph --out data

# 3. Serve the API and UI together on one port.
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

## The scenic score, and why it is a function of time

```
scenic(s, t) = static(s) x daylight(t)  +  golden_hour(t) x faces_the_sun(s, t)
```

`static(s)` blends, all percentile-scaled against the whole network:

| Input | Weight | Source |
|---|---|---|
| curviness | 0.40 | measured lean change per ridden km, blended with road geometry by crowd confidence |
| road class | 0.25 | a motorway is fast, safe and completely unscenic |
| elevation | 0.15 | Copernicus DEM not wired yet; currently the trips' own GPS altitude |
| flow | 0.12 | `1 - share of samples crawling` — the brief's "standstills" red flag |
| 50–120 km/h share | 0.08 | the brief's green-flag speed band |
| tunnel | ×0.65 | no view inside a tunnel |

The time term is real solar geometry (`engine/sun.py`, NOAA approximation, no
API and no dependency). Two things move with the clock:

- **Light.** Scenery is worth little after dark, so the whole score is scaled by
  a daylight factor — 1.0 in daylight, 0.55 at civil twilight, 0.12 at night.
- **Golden hour, directionally.** Between about −4° and +12° sun elevation, a
  segment gets a bonus proportional to how squarely it *points at* the low sun,
  using the segment's mean bearing against the solar azimuth.

Measured on the same request at different departure hours:

| Departure | Sun elevation | Phase | Best route scenic |
|---|---|---|---|
| 09:00 | 21.1° | day | 0.392 |
| 13:00 | 45.8° | day | 0.392 |
| 19:00 | 4.6° | golden hour | **0.429** |
| 22:00 | −23.5° | night | 0.040 |

## Layout

```
precompute/   morton.py            encoder ported from BMW's tripViewer, verified
                                   against the dataset's own morton_code column
              fetch_osm.py         Overpass, tiled and cached
              build_osm_graph.py   ways -> segments at junctions, crowd snapped on
              build_graph.py       the older crowd-only graph (fallback, no OSM)
engine/       profile.py     rider profile: taste / capability / context
              scoring.py     scenic score and the cost function
              router.py      Dijkstra, junction costs, both ride modes
              sun.py         solar position, for scenic(t)
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

- **Coverage is one bbox.** 47.60–48.25 N, 11.15–11.80 E — Munich south to
  Tegernsee. A destination outside it returns a clear error rather than a bad
  route. Widen it by re-running `fetch_osm` with `--bbox`.
- **Only 29% of segments carry crowd data.** The rest score from road geometry
  and class via the confidence blend, which is deliberate graceful degradation —
  the UI reports the percentage per route.
- **residential and service roads are excluded** from the Overpass query. They
  multiply the download several times over and are not where anyone rides for
  pleasure, but it means a start point on a quiet street snaps to the nearest
  larger road.
- **Duration is a target for round trips, a preference for A→B.** Dijkstra
  returns the best path; it will not detour purely to consume time, so a 19 km
  A→B request cannot be stretched to two hours. The label reports what was
  actually achieved rather than what was asked for.
- **Junction costs use tag-free fallback priors**, not the measured speed-drop
  delays described in `plan/ALGORITHM.md` §4.
- **Turn-expanded graph not built yet** — U-turns are blocked by refusing to
  re-enter the segment just used, which is weaker than a proper turn expansion.
  One-ways *are* respected.
- **No DEM, land cover or accident data yet.** Elevation is the trips' own GPS
  altitude, so the elevation term is weak outside crowd-covered roads.
