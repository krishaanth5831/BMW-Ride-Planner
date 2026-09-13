# Faster presentation and real BMW data

Prepared against `main` commit `6a8cda4febb9e366907cbcf54ed31af13b9c4458`.
These changes are local; they have not been pushed to GitHub or installed on
your laptop. No raw BMW dataset is included in the patch.

## Apply on your Windows/WSL laptop

Save `bmw-fast-loading.patch` to your Windows Downloads folder. Stop the running
server with Ctrl+C, then check the project:

```bash
cd "/home/fpvmaster/projects/BMW-Ride-Planner"
git status
```

If there are uncommitted changes, save them on your own branch before continuing.
With a clean working tree, create a feature branch and check the patch first:

```bash
git switch -c alex-performance
git apply --check "/mnt/c/Users/user/Downloads/bmw-fast-loading.patch"
git apply "/mnt/c/Users/user/Downloads/bmw-fast-loading.patch"
```

If the check reports an error, stop: your checkout may have changed since the
patch's base commit. Do not force it or reset your work.

## Run immediately, using the existing sample mode

```bash
python3 -m engine.demo_server --warmup
```

Open <http://127.0.0.1:8000/ride> after the terminal says **ready**. Warmup prepares
the road graph, rider profiles and forecasts before the audience sees the page.
It does not preselect a canned route: route variation and live calculations remain.
The first preparation is slower than subsequent launches; keep this process
running throughout the presentation. Restart it after rebuilding the crowd graph
or changing input files or configuration.

## Use your known recordedTrips folder

This uses every valid trip in that folder for rider A. It does not pretend that
one rider's folder is the whole anonymous crowd dataset or invent riders B/C.

```bash
python3 -m engine.demo_server --warmup \
  --rider-a "/mnt/c/Users/user/Desktop/BMW Motorrad/recordedTrips"
```

The terminal prints each rider's source and actual trip count. Without other
configured rider folders, B and C remain explicitly labeled samples. If a crowd
graph already exists, it is also loaded; otherwise crowd scoring is neutral.
`BMW_RIDER_B` and `BMW_RIDER_C` can point to their corresponding individual folders.

## Incorporate the full dataset once, before the presentation

Find the parent folder containing `anonymizedDataLake`, `exampleUserA`,
`exampleUserB` and `exampleUserC`. The `recordedTrips` folder is not that parent.
Replace the placeholder below with that folder's actual WSL path:

```bash
export BMW_DATASET="/actual/path/to/datasetHackathon"
python3 -m precompute.build_cell_graph --dataset "$BMW_DATASET" --out data
python3 -m engine.demo_server --warmup
```

The full import can take a substantial time and memory, depending on the number
of files and laptop. Do not start it during the live presentation. It scans all
CSV files and shards recursively, without the old fixed 96-shard subset. It
writes `data/cell_graph.json` atomically only after validating a nonempty graph.
No raw files are moved or modified. A `--limit` option exists only for explicitly
requested development subsets; omit it for the complete dataset.

On WSL, many small-file reads can be slower from `/mnt/c/...`; if time and disk
allow, an additional dataset copy in the Linux filesystem can help preprocessing.
Do not delete the original dataset. Subsequent page loads use local caches and
aggregates rather than repeatedly scanning the full lake.

Open <http://127.0.0.1:8000/api/demo/status> to verify:

- `mode`: `telemetry` when a real crowd graph is loaded.
- `ingestion.files_processed`: number of CSV files scanned by the new importer.
- `ingestion.file_limit`: `null` for an unlimited import.
- `crowd_cells` and `crowd_transitions`: actual graph size.
- `riders`: separate real/sample provenance and trip counts for A, B and C.

Old generated graphs may not have ingestion metadata. Rebuild them to obtain it.
Graph mode and rider mode are independent: a real crowd graph does not prove
that an individual rider's profile is real, or vice versa.

The full FastAPI application still works and retains `/`, `/cells`, `/ride`,
uploads, point-to-point planning and the heatmap. For those extra surfaces:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn engine.api:app --host 127.0.0.1 --port 8000
```

Legacy `/` route planning also needs its separate `segments.json` / `graph.json`
build; follow the README's `precompute.build_osm_graph` instructions for that
surface. The `/ride` product UI uses `cell_graph.json` with either launcher.

## What was slow and what changed

| Work | Original implementation | New implementation |
|---|---|---|
| Snapping attractions to roads | Full node scan for each attraction | Exact bounding-box index with identical tie-breaking |
| Repeated route-search legs | Recalculate geometry/risk/cost for the same edges | Request-local exact cost cache, cached identical legs and prebuilt adjacency |
| Restarting the application | Rebuild OSM segmentation and curviness | Persistent, source-versioned JSON cache |
| Simultaneous first requests | Could duplicate expensive graph construction | Locked single initialization |
| Rider history | Parse again after restart; default cap of 60 files | All files, compact numeric arrays, source-versioned profile cache |
| Weather | Network request before trying a disk cache | Fresh-cache first: 15 minutes forecast, 5 minutes current conditions |
| Offline weather retries | Repeat the full timeout | 60-second failed-request cooldown; stale forecasts remain labeled |
| Elevation | Wait for external DEM before returning the route | Show the route immediately, then update elevation asynchronously |
| Switching riders | Sequential requests; late responses could overwrite selection | Parallel profile/weather loading and stale-response guards |
| Full crowd import | Fixed shard subset and hard-coded root | Explicit dataset path and recursive, unlimited-by-default discovery |

Caching does not reduce route candidates, stop limits, the POI list, geometry
resolution, existing scoring rules, or the existing lean-exclusion rule. DEM
elevation remains enabled; only its arrival is deferred in the UI. The older
synchronous API behavior remains available by leaving `terrain` enabled.

Also repaired two existing main-branch merge errors: missing avoidance/penalty
arguments in the legacy Dijkstra method, and one `_in_bbox` definition replacing
another incompatible signature. The original avoidance test now passes.

## Measurements and validation

Measured locally with Python 3.12, the committed Bavaria roads, sample riders,
150-minute requests, fixed seed 42, and network elevation disabled **in both
versions**. These timings are not a promise for your laptop or the full dataset.

| Operation | Original main | Optimized |
|---|---:|---:|
| Runtime initialization | 10.38 s | 1.67 s with the persisted OSM cache |
| Rider A route | 6.09 s | 1.56 s |
| Rider B route | 8.22 s | 1.15 s |
| Rider C route, including fallback search | 34.52 s | 3.44 s |

The complete JSON route responses for A/B/C match the original version's SHA-256
hashes exactly, not merely their route lengths. The first uncached preparation
still does real work; one preliminary uncached run took about 6 seconds.

Run the checks yourself:

```bash
python3 -m unittest discover -v
python3 -m tests.test_avoidance_routing
python3 scripts/benchmark_demo.py
```

Install `requirements.txt` and `httpx` to include the optional FastAPI HTTP tests.
The suite covers golden route responses, exact spatial search, cached-profile
invalidation, more than 60 rider files, all-shard ingestion, safe failed builds,
cache freshness/cooldown, concurrent startup and both HTTP adapters. Inline
JavaScript syntax was checked separately. A visual browser preview was blocked
from reaching this environment's local test server; manually check the page on
your laptop before presenting.

## What the demo data actually is

- Roads: 65,718 subdivided segments from the committed OpenStreetMap extract.
- Scenic attractions: 450 road-accessible points retained by the current index.
- Original lightweight rider profiles: hard-coded sample aggregates and ratings
  in `engine/demo.py`, not the raw BMW CSVs.
- Original lightweight crowd layer: empty; unridden roads used neutral scores
  and assumed road-class speeds. It did not load BMW telemetry automatically.
- New real-data mode: measured rider statistics and precomputed crowd aggregates,
  while road geometry still comes from OSM and forecast/elevation from Open-Meteo.

Full-data ingestion does not mean unlimited geography: the existing crowd
region gate and BMW quality filters still apply, and the committed OSM routing
extract covers Munich and the southern foothills. Existing per-cell percentile
aggregation retains its capped samples; these optimizations do not change that
algorithm into an exact all-sample percentile computation. A/B/C personas, scenic
weights and inferred capability remain heuristics, not a trained model or a
safety certification.

The raw full dataset was not available in this workspace, so its import and
memory usage still need testing on your laptop. Map tiles, external JavaScript,
fonts and satellite imagery still depend on their providers/network; no tiles
were bulk-downloaded. Visit the page and try each planned demo interaction once
before the presentation. Keep the dataset and generated `data/` out of git.
