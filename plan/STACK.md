# Tech Stack

Every choice below was **verified installable on the dev box** — `duckdb`,
`fastapi`, `uvicorn`, `numpy` and `pyarrow` all have cp314 wheels for the
installed Python 3.14.6, and Node v20.20.2 / npm 10.8.2 are present. No conda
environment, no dependency gamble.

```bash
pip install duckdb fastapi uvicorn numpy pyarrow
npm create vite@latest web -- --template react
```

---

## The stack

| Layer | Choice | Why this one |
|---|---|---|
| **Data** | **DuckDB + Parquet** | The entire cell aggregation is *one SQL query*. `read_csv('**/*.csv', filename=true)` globs all 85,699 files and `lag(sensorsbankingangle) OVER (PARTITION BY filename ORDER BY timestampinmillis)` gives Σ\|Δlean\| directly. Spills to disk (only ~6 GB RAM free). Replaces hundreds of lines of multiprocessing — and columnar storage + morton-prefix pruning **is** the scalability answer BMW grades |
| **API** | **FastAPI + uvicorn** | Auto-generated OpenAPI docs double as the "algorithm walk-through" artefact the brief asks for |
| **Routing** | **Python `heapq` Dijkstra** over a Parquet edge list | ~300k nodes / ~1M edges in well under a second. No scipy dependency |
| **Frontend** | **Vite + React (JSX) + Leaflet + Tailwind + Recharts** | Seven dashboard views need real state management; Recharts gives the Learnings progression charts nearly free |
| **Map** | **Leaflet**, tiles **pre-cached locally** | Same library BMW's own viewer uses, so its morton grid/heat layer ports straight over |
| **Weather** | **Open-Meteo** | No API key. Fetched once, cached to disk, frozen fallback |
| **Geo POIs** | **Overpass** → `fixtures/` | Water, forest, viewpoints, `maxspeed`. Fetched once. **Never called live during the demo** |
| **Sun position** | **Local NOAA formula** | ~20 lines, zero dependencies, powers the sunset KPI |
| **Trip tags** | **Local JSON file** | Emoji/emotion tagging needs persistence, not a database |
| **Traffic** | **No API** — crowd temporal prior | No free keyless real-time source exists. Deriving congestion from BMW's own 85,699 trips is *stronger* than a stubbed API and earns another crowd-data point |

### JSX, not TypeScript

Types are friction at 02:00 with no payoff by 09:00. This is a one-night build
with a fixed end date, not a codebase we maintain.

### Why React rather than plain JS

The dashboard spec in [UI.md](UI.md) has **seven views** with cards, charts, a
fog map and a stats page. Vanilla state management across seven views is where
the night gets eaten; `npm create vite` costs two minutes.

---

## Repo layout — one owner per directory

```
precompute/    DuckDB SQL + runner → data/cells.parquet, data/edges.parquet
engine/        FastAPI: scoring, Dijkstra, joyride, profile fitting, suggestions
web/           Vite + React dashboard
fixtures/      frozen weather / OSM / demo routes / map tiles
plan/          these docs
data/          generated artefacts (gitignored)
```

| Directory | Owner |
|---|---|
| `precompute/` | one owner |
| `engine/` | Krish |
| `web/` | one owner |

**Freeze the API contract in the first 30 minutes.** That, not the branch
topology, is what stops three people blocking each other.

Branch workflow: commit straight to `dev`; `main` only via PR; Krish merges.

---

## The `Track` shape — one format for recorded rides and planned routes

This is the unification that makes the ride-preview feature nearly free.

```json
{
  "id": "02cbe62f-d6a6-493a-a4e2-980e7536c3bd",
  "kind": "recorded" | "planned",
  "points": [
    { "lat": 48.166, "lon": 11.6287, "elev": 514.1, "t": 1684569612248,
      "speed": 0.0, "lean": -15.86, "rpm": 2250, "abs": 1 }
  ],
  "kpis": { "fun": 0.72, "scenic": 0.81, "safety": 0.88, "growth": 0.34,
            "km": 94.2, "minutes": 108, "curviness": 412.0, "elev_gain": 840 },
  "stops": [ { "lat": 47.61, "lon": 11.35,
               "type": "viewpoint" | "water" | "coffee", "name": "Kesselberg" } ],
  "why": { "weights": { "curviness": 0.41, "altitude": 0.22, "water": 0.18,
                        "flow": 0.19 },
           "alternative": { "minutes": 94, "curviness": 128, "elev_gain": 500 },
           "confidence": 0.86 }
}
```

- **Recorded** → `GET /trips/{id}/track` reads that trip's CSV through DuckDB.
  Real telemetry, real gauges.
- **Planned** → `POST /route` returns the same shape, with `speed` and `lean`
  filled from **crowd-predicted per-cell values** along the chosen path.

One renderer, one playback component, one set of gauges. So *"view the ride
before you even go on it"* costs almost nothing, and most of the code is BMW's
own (`app.js` already has playback with lean/accel/speed gauges).

---

## API surface

```
GET  /riders                        A, B, C (+ any imported)
GET  /riders/{id}/profile           weights, lean envelope, bike class, free-time histogram
GET  /riders/{id}/trips             list + per-trip KPIs
GET  /riders/{id}/coverage          visited level-14 cells → fog map
GET  /riders/{id}/learnings         progression series + records + next challenge
GET  /riders/{id}/suggestions       cards for the Suggestions view
POST /riders/import                 point at a folder of recordedTrips
GET  /cells?bbox=&level=&metric=    crowd area-metrics layer
GET  /trips/{id}/track              recorded Track
POST /route                         {rider, from, to, start_time, alpha, budget} → 3 Tracks
POST /joyride                       {rider, origin, hours, start_time, alpha}   → 3 Tracks
POST /trips/{id}/tag                emoji / emotion
```

---

## Running it

```bash
# terminal 1
uvicorn engine.api:app --port 8000

# terminal 2
cd web && npm run dev
```

**Demo on the dev server, not a production build.** Same Leaflet, works offline
once tiles are local, and there is no build step that can fail at 08:15.

---

## Demo hardening

Venue wifi is the most common way a live demo dies. All of this must be done
well before the pitch, not improvised on the morning.

- **Map tiles** — pre-download Munich + foothills, z10–z14, into `web/tiles/`
  and serve locally. **Verify with the network interface switched off.**
- **Weather** — Open-Meteo fetched once into `fixtures/`, with a frozen fallback
  the app uses on any network error.
- **OSM POIs** — one Overpass query, cached. Never live.
- **Scripted scenarios** — pre-compute and cache the 2–3 demo routes to disk.
  Live compute stays the default path; the cache is the parachute.
- Everything on localhost. The whole demo must run with networking disabled.
