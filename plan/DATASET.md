# The Dataset — what is actually in it

Measured, not assumed. Read this before writing any feature. Everything below
was verified by sampling the real files on 2026-09-12.

Location: `~/Desktop/BMW/exd_download/datasetHackathon/` (outside this repo —
14 GB, never commit it).

---

## What we were given

| Path | Contents |
|---|---|
| `anonymizedDataLake/trips-samples-{1,2}/{00..ff}/*.csv` | **85,699 crowd trips**, 13 GB, sharded by morton prefix. ~1 Hz sampling, median ~310 rows (~5 min snippets) |
| `exampleUserA/recordedTrips/*.csv` | **101** full trips (up to 17,702 rows ≈ 5 h) |
| `exampleUserB/recordedTrips/*.csv` | **73** full trips |
| `exampleUserC/recordedTrips/*.csv` | **224** full trips |
| `exampleUserA/plannedRoutes/*.gpx` | **89** routes planned in the BMW app |
| `tripViewer/tripViewer/` | BMW's own Leaflet viewer — `app.js`, `index.html`, `style.css` |

All CSVs share one 42-column schema. Date range spans **2024-04 → 2026-09** in
the lake; rider A's own trips run **2021-07 → 2026-08**.

---

## Column fill rates

Measured over 150 sampled lake trips and every trip of riders A, B and C.

| Column | Lake | A | B | C | Verdict |
|---|---:|---:|---:|---:|---|
| `sensorsbankingangle` | 100% | 89% | 83% | 100% | **The moat.** Lean, curviness, capability, growth |
| `positionmapmatchedlatitude/longitude` | 100% | 99% | — | — | Map-matched geometry — use for routing |
| `positionrawlatitude/longitude` | 100% | 100% | — | — | Raw fallback |
| `positionrawelevation` | 100% | 99% | 100% | 100% | **Use this** for altitude |
| `positionmapmatchedelevation` | 46% | 70% | — | — | Too sparse — **do not use** |
| `ridingvehiclespeed` | 88% | 55% | 70% | 80% | Speed KPIs; fall back to `positionrawspeed` |
| `positionrawspeed` | 89% | 55% | — | — | Fallback |
| `ridingabsbraking` | 99% | 88% | 86% | 99% | Road-condition + safety signal |
| `ridingasccontrol` | 98% | 88% | — | — | Traction-control events |
| `ridingenginespeed` | 97% | 82% | — | — | Engagement; bike-class inference |
| `ridinggear` | 74% | 87% | — | — | Bike-class inference |
| `ridingthrottlevalue` | 73% | 64% | — | — | Usable with care |
| `sensorsaccelerationlongitudinal` | 99% | 78% | — | — | Braking proxy |
| `sensorsoutsidetemperature` | 94% | 88% | — | — | "Conditions ridden in" (rider history) |
| `sensorstirepressurefront/rear` | 88% | 87% | — | — | Weak bike-class hint |
| `ridingtotalmileage` | 100% | 100% | — | — | Odometer |
| `sensorsenginetemperature` | 35% | 73% | — | — | Low value |
| `energy*` | 0–7% | 0–18% | — | — | Effectively empty |
| `sensorsaccelerationlateral` | 70% | **3%** | **5%** | 73% | ⚠ Crowd-only — see below |
| `sensorsaccelerationvertical` | 70% | 3% | — | — | ⚠ Same problem |
| `sensorsbreakpressurefront/rear` | **0%** | **0%** | **0%** | **0%** | ☠ **Dead column** |
| `trip_id` | 56% | 68% | — | — | ⚠ Unreliable — see below |

### Three traps

**☠ `sensorsbreakpressurefront/rear` is 0% filled everywhere.** Our whiteboard
notes list "braking" as a profiling signal. It has to come from
`ridingabsbraking` (99%) plus `sensorsaccelerationlongitudinal` (99%) instead.
Anyone who builds against brake pressure loses the time silently — there is no
error, just zeros.

**⚠ `sensorsaccelerationlateral` works on crowd data and fails on riders.** 70%
in the lake but **3% for rider A and 5% for rider B**. It must **never** enter
the personal rider profile: it would pass every test against rider C and then
return nothing for two of three real riders.

**⚠ `trip_id` is only 56% filled in the lake.** Use **the filename** as the trip
identifier. This matters most in the aggregation query — partitioning a window
function by `trip_id` drops ~44% of rows into one null partition and corrupts
every Δlean across trip boundaries. Use
`read_csv('.../**/*.csv', filename=true)` and partition by `filename`.

---

## Three things the notes claim that the data does not contain

"Usage of BMW Personal Rider Data" is a graded criterion, so claiming a field we
do not have is the one thing that reads as fabrication to a BMW engineer.

| Claimed | Reality | What we do instead |
|---|---|---|
| Rider **age** | Not present | Profile is behavioural only |
| **Bike model** | Not present | Infer a coarse class from rpm-per-km/h, gear count, lean envelope, tyre pressure — and let the rider confirm it in the UI |
| **Road construction** | No live source | Use OSM `highway=construction` from the cached extract, or drop the KPI. Do not imply it is live |

---

## Geography

The crowd lake concentrates tightly around **Munich and its southern
foothills — 47.6–48.5 N, 11.0–11.9 E**. Rider A's personal trips range much
wider (43.6–48.9 N, 4.8–13.4 E: the Alps, Dolomites, South Tyrol), but the
*crowd graph* is Bavarian.

Demo targets, taken from rider A's own planned-route filenames:
**Kesselberg, Tegernsee, Ammersee, Sylvenstein, Fischbachau**.

On stage: *"BMW's crowd data is Bavarian, so that's where we demo."* A strength,
not an apology.

---

## The planned routes — BMW's own preference vocabulary

`exampleUserA/plannedRoutes/*.gpx` carry a BMW extension namespace:

```xml
<cnrd:PlannedRouteExtension durationInSeconds="11461" lengthInMeters="131115">
  <cnrd:routeOptions optimization="winding" windingness="medium" hilliness="ignore">
    <cnrd:avoidances ferries="allow" tunnels="allow" dirtRoads="forbid"
                     carTrains="forbid" borderCrossings="allow" motorways="allow"
                     tollRoads="allow" alreadyUsedRoads="allow"/>
```

Use their words in our UI and API — `windingness`, `hilliness`,
`alreadyUsedRoads` — so the solution reads as an extension of their product
rather than a bolt-on. (`alreadyUsedRoads` is exactly the reuse penalty the
joyride loop needs.)

### The narrative statistic

Of the 89 GPX files, **74 carry route options** (15 have none). Of those 74:

- **69 (93%) are set to `optimization="fastest"`** — only 5 to `"winding"`
- **`windingness` sits on the `medium` default in every single one** — never touched
- `lengthInMeters` is present in only 22 files; where present, routes run
  43–343 km, median ~127 km

Yet his lean traces show a rider hunting corners.

> The rider told the app he wanted the fastest route 93% of the time. His bike
> says otherwise. **We don't ask riders what they want — we read it off the
> motorcycle.**

---

## BMW's sample viewer — reuse, don't re-derive

`tripViewer/tripViewer/app.js` is explicitly labelled "quick & dirty… not the
expected starting point", but it contains four things worth taking verbatim:

| What | Where | Why |
|---|---|---|
| **Morton encoder** | `app.js:225` | Equirectangular quadkey — `xf=(lon+180)/360`, `yf=(lat+90)/360`. **Not** Web Mercator. Rolling our own misplaces every cell |
| **Curviness definition** | `app.js:188` | Lean change per km, 2° noise floor |
| **GPS gap guard** | `app.js:176` | `MAX_GAP_M` — the dropout filter our edge builder needs |
| **Playback + gauges** | playback section | Live lean / accel / speed gauges — the ride-preview hero comes almost free |

It also ships an experimental `funFactor` (`app.js:203`) that **multiplies by
`pathKm`**, so a cell scores higher simply for containing more road, regardless
of quality — an exposure count hiding inside a quality score. We normalise per
kilometre and use trip count only as a confidence weight. Naming that difference
on stage is the strongest signal that we actually read their data.

---

## Environment facts (verified on the dev box)

- Python **3.14.6** (miniforge). `duckdb`, `fastapi`, `uvicorn`, `numpy`,
  `pyarrow` **all have cp314 wheels** — no conda env needed.
- Node **v20.20.2**, npm **10.8.2**.
- **16 cores**, ~6 GB RAM free of 29 GB → the aggregation must spill to disk.
  DuckDB does this natively.
