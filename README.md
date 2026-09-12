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

**Routing runs on the scenic score alone** — no fun, growth or taste weighting.
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

## Quick start

```bash
pip install duckdb fastapi uvicorn numpy pyarrow python-multipart

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
