# BMW Ride Planner

A ride planner built for motorcyclists, not for commuters.

Most routing software answers *"what is the fastest way from A to B?"* That is
the wrong question for a rider on a Sunday morning. This answers a different
one: **what is the best ride I can have today?** It picks a weather window,
works out what this rider is capable of from their own telemetry, and routes
along roads that are twisty, scenic and free-flowing, while never sending them
onto roads the crowd rides harder than they do.

Built in one weekend for the **BMW Motorrad track ("FIND YOUR THRILL — Route
Challenge") at the TUM.ai EHL hackathon in Zurich**, pitched on 2026-09-13.
It is a hackathon project: it runs and demos well, but it is not maintained
as a product.

- [`DEMO.md`](DEMO.md): the five-minute pitch walkthrough
- [`plan/`](plan/): design docs, start with [`plan/ALGORITHM.md`](plan/ALGORITHM.md)
- [`PERFORMANCE.md`](PERFORMANCE.md): caching work and the full-dataset import
- [`MORTON_ROUTER.md`](MORTON_ROUTER.md): the original "morton square" router, kept for comparison at `/cells`

---

## Quick start (no dataset, no installs)

You need Python 3 (developed on 3.12, also runs on 3.14). Nothing else: the
quick demo uses only the standard library.

```bash
git clone https://github.com/krishaanth5831/BMW-Ride-Planner.git
cd BMW-Ride-Planner
python3 -m engine.demo_server --warmup
```

Wait for `BMW Ride Planner ready: http://127.0.0.1:8000/ride`, then open
<http://127.0.0.1:8000/ride> in your browser.

What you get in this mode:

- Real roads from the committed OpenStreetMap extract (`fixtures/osm/`), Munich
  south to Tegernsee.
- Three example riders **A / B / C** with clearly labelled sample values (no
  BMW telemetry is bundled).
- A live weather forecast from Open-Meteo (no key needed). Offline, it falls
  back to bundled sample weather and says so.

`--warmup` builds the road graph, rider profiles and forecasts before the page
is announced, so the first click is fast. The first run is slower; later runs
reuse caches in `data/cache/`. Other flags: `--host`, `--port`, `--dataset`,
`--rider-a`, `--cell-graph` (see below).

Check what the server is actually using at
<http://127.0.0.1:8000/api/demo/status> (`mode` is `telemetry` when a real
crowd graph is loaded, `sample` values are labelled per rider).

## The three pages

| Page | Served by | What it is |
|---|---|---|
| `/ride` | both servers | The product a rider sees: today's weather window, their profile, a planned ride with named stops, corner and elevation stats |
| `/cells` | FastAPI only | The engineering view: the morton-square router next to the road router, the raw graph, every number |
| `/` | FastAPI only | The first planner: upload telemetry, round trip / point to point, and the X→Y heatmap |

The quick-start server (`engine.demo_server`) only serves `/ride`. The
**post-ride summary** button on `/ride` calls `/api/ride/replay/{rider}`, which
only the FastAPI app provides.

## Full setup (BMW telemetry)

The telemetry-backed mode needs the BMW hackathon dataset (about 20 GB), which
was handed out to participants and is **not** in this repo. It is expected at
`~/Desktop/BMW/exd_download/datasetHackathon`; point elsewhere with
`BMW_DATASET=/path/to/datasetHackathon`. The folder must contain
`anonymizedDataLake`, `exampleUserA`, `exampleUserB` and `exampleUserC`.

```bash
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt  # fastapi, python-multipart, uvicorn
python -m pip install duckdb               # needed by the precompute steps and uploads
```

### For `/ride`: the crowd graph

```bash
# Scans every trip once and writes data/cell_graph.json (slow, run it before the demo).
python -m precompute.build_cell_graph --dataset "$BMW_DATASET" --out data

python -m engine.demo_server --warmup
```

With the dataset present, riders A/B/C use their real `recordedTrips`
folders. You can also point one rider at a single folder:
`--rider-a /path/to/recordedTrips` (or `BMW_RIDER_A`, `BMW_RIDER_B`,
`BMW_RIDER_C`). A configured folder with no valid trips is an error, never a
silent switch to sample data.

### For `/` and `/cells`: the road graph and the FastAPI app

```bash
# 1. Road network from Overpass (~5 min, tiles cached in fixtures/osm/).
#    Optional: the combined ways_*.json is already committed.
python -m precompute.fetch_osm

# 2. Split ways at junctions and snap the crowd telemetry onto them (~5 min).
#    Writes data/segments.json and data/graph.json.
python -m precompute.build_osm_graph --out data

# 3. Serve the API and all three pages on one port.
python -m uvicorn engine.api:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>, then upload `.csv` files (a folder, single
files or a `.zip`) or click **A** / **B** / **C** for a bundled example rider.

Optional extras:

- `python -m precompute.fetch_scenic` refreshes the scenic points (lakes,
  passes, peaks, viewpoints). The result is already committed in
  `fixtures/scenic/`.
- `python -m precompute.fetch_dem` downloads a Copernicus elevation raster for
  the scenic score's terrain term. It needs `numpy` and free Copernicus Data
  Space credentials (`COPERNICUS_CLIENT_ID`, `COPERNICUS_CLIENT_SECRET` in
  `.env`). Without it, the score falls back to the trips' own GPS altitude.

## Configuration

**No API keys are needed.** Copy [`.env.example`](.env.example) to `.env` only
if you want the optional extras. The FastAPI app reads `.env` itself.

| Variable | Used for |
|---|---|
| `BMW_DATASET` | Dataset location (default `~/Desktop/BMW/exd_download/datasetHackathon`) |
| `BMW_RIDER_A` / `_B` / `_C` | One `recordedTrips` folder for one rider |
| `BMW_CELL_GRAPH` | Crowd graph file (default `data/cell_graph.json`) |
| `BMW_CACHE_DIR` | Derived caches (default `data/cache/`) |
| `STADIA_API_KEY`, `GEOAPIFY_API_KEY` | Nicer basemap tiles; without them the map uses CARTO, then plain OSM |
| `COPERNICUS_CLIENT_ID`, `COPERNICUS_CLIENT_SECRET` | Only for `precompute.fetch_dem` |

`HERE_API_KEY` / `TOMTOM_API_KEY` appear in `.env.example` for a live-traffic
layer that was designed but is not wired up; traffic comes from the fleet's own
observed speeds instead.

## How a ride is chosen

The rider profile is used only for things that are not preferences: the
capability ceiling (a hard exclusion), the start point, and the default ride
length. Everything else is derived from the uploaded data:

| Derived | From |
|---|---|
| Where the ride starts | the start point of their own most frequent trips |
| How long it runs | the median duration of their recorded rides |
| What counts as "good" | revealed preference: which roads they chose, vs the crowd |
| What is too much | road-normalised style ratio → a hard capability ceiling |
| Bike class | rpm per km/h, gear count, lean envelope (inferred, confirmable) |

**Safety is a hard exclusion, not a penalty.** A road the crowd rides with more
lean than this rider's own envelope allows is removed from their graph. No
scenic or fun bonus can buy it back.

Weather windows come from the Open-Meteo forecast and are cached locally. Each
day is evaluated from three hours before local sunset until one hour after; a
ride is confirmed when every hourly score passes and no hour is above 30°C.

Routes use major roads to leave Munich, then ramp in a strong motorway/trunk
penalty from 20–25 km out from Marienplatz. Those roads stay available when
they are the only practical connection. Long, straight named roads get an
extra geometry-based cost outside the same radius.

### The scenic score, and why it depends on the time of day

```
scenic(s, t) = static(s) x daylight(t)  +  golden_hour(t) x faces_the_sun(s, t)
```

`static(s)` blends inputs that are each percentile-scaled against the whole
network. The weights live at the top of `engine/scoring.py` (as of this
README):

| Input | Weight | Source |
|---|---|---|
| curviness | 0.28 | measured lean change per ridden km, blended with road geometry by crowd confidence |
| road class | 0.22 | a motorway is fast, safe and completely unscenic |
| elevation | 0.15 | Copernicus DEM where fetched, otherwise the trips' own GPS altitude |
| relief | 0.15 | local height difference from the DEM |
| forest | 0.10 | share of forest along the segment |
| water and viewpoints | 0.10 | closeness to water, blended with nearby viewpoints |
| tunnel | ×0.65 | no view inside a tunnel |

Flow, gradient and the 50–120 km/h speed band are wired in but currently
weighted 0.

The time term is real solar geometry (`engine/sun.py`, NOAA approximation, no
API). Scenery is scaled by daylight (1.0 day, 0.55 civil twilight, 0.12 night),
and in golden hour a segment gets a bonus for how squarely it points at the low
sun, so the same request can score differently at 13:00 and at 19:00.

### X→Y heatmap (on `/`)

Choose **X → Y heatmap**, click A and B on the map, pick a 30/50/100 km detour
radius and build. The API is `POST /api/heatmap` (alias `/api/plan/heatmap`)
with the same telemetry form data as `/api/plan/upload` plus `origin_lat`,
`origin_lon`, `dest_lat`, `dest_lon`. It samples up to 30 distinct routes,
scores each for scenic, fun and personal fit, and draws a segment heatmap:
greener means higher score, thicker means more candidate routes use it.
Points outside the Bavaria bbox return `422`.

Heatmap routing adds three cost multipliers on travel time, so every edge cost
stays positive and Dijkstra stays valid:

- **Highways are avoided, not forbidden.** Motorways 5×, motorway links 4×,
  trunks 3×, trunk links 2.5×, primary 1×, primary links 0.8×.
- **Long, straight roads are penalised** by the length of the whole road
  (`road_run_km`), scaled by how *un*-twisty it is. A 14 km alpine pass is
  untouched; a 14 km straight Bundesstrasse is expensive.
- **Traffic pressure comes from BMW crowd telemetry** (`crawl_share` and the
  inverse of `band_share`), blended with a road-class prior where the crowd has
  no data. It raises cost; it never excludes a road.

The legacy round-trip and point-to-point modes leave these at `0.0`.

### Where the numbers come from

Anything marked **(\*)** in the code is a value we chose ourselves, not from the
BMW dataset, an API or the brief. The inputs are measured; the scoring is our
opinion, and every weight lives in one file you can argue with. Full inventory:
[`plan/PROVENANCE.md`](plan/PROVENANCE.md).

## Tests

```bash
python3 -m unittest discover -v          # 27 tests, under a minute
python3 scripts/benchmark_demo.py        # route timings for riders A/B/C
python3 scripts/demo_cell_router.py      # morton router end to end (needs data/cell_graph.json and
                                         # the dataset unpacked at exd_download/datasetHackathon/ in the repo)
```

The FastAPI HTTP test also needs `requirements.txt` plus `httpx`. All 27 pass
on a fresh clone. With a locally built `data/cell_graph.json` present (telemetry
mode), `test_demo_ratings.test_seeded_variations_are_reproducible_and_diverse`
currently fails (the tests appear to pick up the local data and caches).

The morton encoder has its own correctness gate; run it before trusting any
precompute output:

```bash
python -m precompute.morton \
  "$BMW_DATASET/exampleUserA/recordedTrips/02cbe62f-d6a6-493a-a4e2-980e7536c3bd.csv"
```

## Layout

```
engine/       demo_server.py   stdlib server for /ride (the quick start)
              api.py           FastAPI app: /, /cells, /ride and all /api routes
              ride_data.py     dataset / rider / crowd-graph discovery and provenance
              rider_profile.py telemetry -> rider profile (cached)
              profile.py       rider profile: taste / capability / context
              demo.py          labelled sample riders for the dataset-free mode
              cell_osm_router.py  /ride planner: OSM roads scored with crowd cells
              cell_router.py   morton-square router (MORTON_ROUTER.md)
              router.py        segment router behind / : Dijkstra, both ride modes, heatmap
              scoring.py       scenic score and the cost function
              scenic.py        scenic points and stops
              rideworthy.py    weather windows
              weather.py       Open-Meteo, cached, labelled fallback offline
              terrain.py       corners and elevation for the chosen ride
              replay.py        post-ride summary from recorded telemetry
              sun.py           solar position, for scenic(t)
              spatial.py, cache.py, net.py   lookup index, disk caches, HTTPS
precompute/   morton.py            encoder ported from BMW's tripViewer, verified
                                   against the dataset's own morton_code column
              build_cell_graph.py  crowd graph -> data/cell_graph.json (stdlib only)
              fetch_osm.py         Overpass, tiled and cached
              build_osm_graph.py   ways -> segments at junctions, crowd snapped on
              build_graph.py       the older crowd-only graph (fallback, no OSM)
              fetch_scenic.py      scenic points from Overpass
              fetch_dem.py         Copernicus DEM raster (optional)
fixtures/     osm/, scenic/        committed road and scenic extracts
web/          ride.html, cells.html, index.html + app.js, style.css
scripts/      benchmark_demo.py, demo_cell_router.py
tests/        unittest suite
plan/         design docs
data/         generated graphs and caches (gitignored)
```

## Known gaps

- **Coverage is one bbox.** 47.60–48.25 N, 11.15–11.80 E, Munich south to
  Tegernsee. A destination outside it returns a clear error. Widen it by
  re-running `fetch_osm` with `--bbox`.
- **Only part of the network carries crowd data.** The rest scores from road
  geometry and class through a confidence blend; the UI reports the share per
  route.
- **Residential and service roads are excluded** from the Overpass query, so a
  start on a quiet street snaps to the nearest larger road.
- **Duration is a target for round trips, a preference for A→B.** Dijkstra will
  not detour just to use up time; the label reports what was achieved.
- **Junction costs use fallback priors**, not the measured speed-drop delays in
  `plan/ALGORITHM.md` §4.
- **No turn-expanded graph yet.** U-turns are blocked by refusing to re-enter
  the segment just used. One-ways are respected.
- **No push notifications.** `/api/ride/windows` returns the payload a
  notification would carry; it is shown in-app only.
- Sample riders, scenic weights and the inferred capability ceiling are
  heuristics, not a trained model and not a safety certification.

## Team

Built by **Krishaanth Ramaraj** ([@krishaanth5831](https://github.com/krishaanth5831)),
**Nikolay Didiletsov** (Niko) and **Aleksandar Yordanov** (Alex).

Branches: `main` is the demo-safe branch and only takes pull requests; work
lands on `dev` (and personal feature branches) and goes to `main` by PR.
