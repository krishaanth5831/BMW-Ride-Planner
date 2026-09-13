# The Route Algorithm

How BMW Ride Planner decides what a good ride is, and how it proves it.

Everything here is grounded in columns that were **measured** in BMW's dataset,
not assumed. Fill rates and dead columns are in [DATASET.md](DATASET.md);
read that before implementing any of this.

---

## 1. The core idea in one paragraph

Ordinary navigation asks *"which way is fastest?"* and adds up minutes. We use
the same search algorithm — Dijkstra — but we **distort the perceived cost of
each road in a controlled, explainable way**. A boring road is told it is longer
than it looks; a great road is told it is shorter. Then we simply ask for the
cheapest path, and what comes back is a good *ride* rather than a fast *trip*.

Everything below is (a) deciding how much to distort, (b) making that distortion
personal to the rider, and (c) being able to show our working afterwards.

The distortion is never allowed to make a road *free* or *negative* — that keeps
Dijkstra provably correct, which matters because we need to defend this on stage.

---

## 2. Pipeline overview

```
  85,699 crowd trips (13 GB CSV)
            │
            │  ① offline aggregation (one DuckDB query)
            ▼
   cells.parquet        one row per ~100 m map square
            │
            │  ② edge construction
            ▼
   edges.parquet        observed square→square transitions
            │
   ┌────────┴─────────────────────────────┐
   │                                      │
   │  ③ rider profile          ④ external enrichment
   │     (personal trips)         (weather, OSM, sun)
   │                                      │
   └────────┬─────────────────────────────┘
            │  ⑤ scoring  →  ⑥ cost function
            ▼
   ⑦ Dijkstra  ──►  Destination mode  /  Joyride mode
            │
            ▼
   ⑧ explanation payload  →  UI
```

Stages ① and ② run **once, offline**. Stages ③–⑧ run **per request**, in well
under a second.

---

## 3. Stage ① — Chop the map into squares and ask the crowd

### Why squares

BMW's dataset already ships a spatial index: every telemetry row carries a
`morton_code`, a 32-digit base-4 quadkey. It is effectively a postcode for a
square on the map, with one very useful property — **squares that are near each
other share a code prefix**. So:

- "everything near here" is a string prefix match (`morton_code LIKE '1220013%'`)
- zooming out is *truncating the string* — no re-aggregation needed

We aggregate at **level 18 ≈ 100 m** for road-quality features, and roll up to
**level 14 ≈ 1.6 km** for anything that needs more samples per bucket.

The encoder is already written and documented in BMW's own sample viewer at
`tripViewer/tripViewer/app.js:225`. **Port it verbatim.** It is an
equirectangular grid — `xf = (lon+180)/360`, `yf = (lat+90)/360`, both axes
divided by 360 — which is *not* Web Mercator. Rolling your own will silently
misplace every cell.

### What we compute per square

One pass over all 85,699 trips. Everything is a running sum, so it streams.

| Feature | How | Column |
|---|---|---|
| `path_m` | Σ haversine between consecutive points | `positionmapmatchedlat/lon` |
| `lean_delta` | Σ \|Δlean\|, ignoring steps < 2° as sensor noise | `sensorsbankingangle` |
| `leaned_m` | distance ridden with \|lean\| > 5° | `sensorsbankingangle` |
| `lean_p50`, `lean_p95` | percentiles of \|lean\| | `sensorsbankingangle` |
| `speed_mean`, `speed_p85` | observed, **not** the speed limit | `ridingvehiclespeed` |
| `band_share` | share of samples in 50–120 km/h | `ridingvehiclespeed` |
| `crawl_share` | share of samples under 20 km/h | `ridingvehiclespeed` |
| `abs_events` | count of ABS activations | `ridingabsbraking` |
| `hard_decel` | count of samples under −3 m/s² | `sensorsaccelerationlongitudinal` |
| `elev_mean/min/max` | elevation | `positionrawelevation` |
| `rpm_mean` | engine speed | `ridingenginespeed` |
| `temp_mean` | ambient — historical weather proxy | `sensorsoutsidetemperature` |
| `n_trips` | distinct rides through this square | **the filename** |

### Curviness — the key measure, and why it is lean-based

Curviness is **degrees of lean change per kilometre**:

```
curviness = lean_delta / (path_m / 1000)
```

This is BMW's own definition (`app.js:188`) and it is cleverer than it looks. A
long constant-radius sweeper holds a near-constant lean angle, so it accumulates
almost no *change*. A proper series of corners swings left–right–left and racks
up change fast. So this measures **the thing riders actually enjoy** — direction
changes — rather than mere road geometry.

Steps below 2° are discarded as sensor noise, otherwise every straight
accumulates a baseline from vibration.

> **This is the moat.** `sensorsbankingangle` is 100% filled in the crowd data,
> and phones cannot measure it. Every competing route planner is inferring
> curvature from map geometry; we are reading it off the motorcycle.

### Implementation note that will bite you

The aggregation is one DuckDB query. Δlean needs a window function:

```sql
lag(sensorsbankingangle) OVER (PARTITION BY filename ORDER BY timestampinmillis)
```

**Partition by `filename`, never by `trip_id`.** `trip_id` is only 56% filled in
the lake — partitioning on it drops ~44% of rows into one giant null partition
and silently corrupts every Δlean across trip boundaries. Use
`read_csv('.../**/*.csv', filename=true)`.

### Traffic, measured instead of guessed

There is no free keyless real-time traffic API, and a key provisioned the night
before a demo is a liability. So we derive congestion **from BMW's own data**:
median speed per square per time band, divided by that square's free-flow p85.

```
congestion(cell, t) = 1 − ( speed_median(cell, band(t)) / speed_p85(cell) )
```

Bucket coarsely or it will be empty. 85,699 trips × ~310 median rows ≈ 27M
points, spread over ~10⁵–10⁶ level-18 squares, is only a few dozen points per
square — split across 24 h × 7 days it is mostly null. So compute the temporal
prior at **level 14** with **weekday/weekend × 3 time bands**. Morton rollup is
string truncation, so this is one extra `GROUP BY` in the same query.

This is strictly better than a stubbed API: it is real rider behaviour, it needs
no network at demo time, and it earns another crowd-data point on the rubric.

---

## 4. Stage ② — Build the road network from the riders themselves

Nodes are squares that riders have actually ridden. Edges are **observed
square→square transitions** in the crowd trips, carrying the observed median
speed for that transition.

We never download a road map. The network *is* the crowd data. Which means:

- we route on roads riders actually ride, at speeds they actually ride them
- coverage is exactly where BMW has riders, and the UI can say so honestly
- zero external dependency in the critical path

### Two filters that are not optional

**1. Drop transitions longer than ~150 m** (1.5× the level-18 cell diagonal).

If a rider's GPS drops out for 30 seconds, the data shows one "transition"
spanning two to five kilometres. Dijkstra will find it and love it — it looks
like a free shortcut with a plausible `time = L/v`. BMW's own viewer guards
against exactly this at `app.js:176` with `MAX_GAP_M`. Without the filter, the
Munich→Kesselberg test can pass while the route quietly teleports.

**2. Require ≥ 2 distinct trips per edge.**

One rider's stray GPS sample should not create a road. This also prunes
*parallel-road collapse*: a motorway and its frontage road can fall inside the
same 100 m square, and only transitions riders actually made survive.

### Verification before trusting the graph

Assert no edge exceeds the threshold, assert every edge has ≥ 2 trips, then
**render the 20 longest edges on a map and look at them**. A teleport that
survives into the demo is worse than a missing feature.

---

## 5. Stage ③ — Learn what this rider likes, from their own bike

### Revealed preference

We take the squares the rider has actually ridden, distance-weight them, and
compare each feature's mean against the crowd baseline:

```
z_R(f) = ( μ_R(f) − μ_crowd(f) ) / σ_crowd(f)      # per feature f
w_R(f) = normalise( clip(z_R(f), 0, ∞) )           # weights, Σw = 1
```

In words: *whatever this rider consistently over-indexes on becomes a heavier
weight in their personal scoring.* If their rides are 1.8× curvier and 300 m
higher than the average BMW rider, curviness and altitude dominate their weights.

Roughly thirty lines of code, no training, no black box, and the resulting
weight vector goes **straight onto a bar chart in the UI**. That is the whole
reason to prefer it over a fitted model: the brief promises bonus points for
explainability, and a judge can audit this on screen in five seconds.

### The lean envelope — the rider's capability

From their own trips: `rider_p90` and `rider_p95` of \|lean\|. This single number
does two jobs:

- **Safety ceiling** (stage ⑥) — the hard limit on what we will route them onto
- **Growth target** (below) — the basis for finding roads that stretch them

### Also derived from personal data

- **Free-time window** — histogram of their trip start times, weekday × hour,
  plus typical duration. Drives the Overview greeting and notifications.
- **Weather history** — distribution of `sensorsoutsidetemperature` across their
  rides, i.e. what conditions they have actually ridden in.
- **Bike class** — inferred from rpm-per-km/h ratio, gear count, lean envelope,
  tyre pressure. **Confirmable by the rider in the UI**, because bike model is
  not in the dataset.

### Three things we must not claim

The dataset does **not** contain rider **age**, **bike model**, or **road
construction**. All three appear in our whiteboard notes. "Usage of BMW Personal
Rider Data" is a graded criterion, and claiming a field we do not have is the
one thing that reads as fabrication to a BMW engineer in the room. Profiles are
behavioural only.

Also: `sensorsaccelerationlateral` is 70% filled on crowd data but **3% and 5%
for two of the three example riders**. It must never enter the personal profile —
it would pass every test and silently return nothing for a real rider.

---

## 6. Stage ④ — External enrichment

| Source | Used for | Demo safety |
|---|---|---|
| **Open-Meteo** | rain, temperature, wind at ride time | no API key; fetched once, cached, frozen fallback |
| **OSM / Overpass** | water, forest, viewpoints, `maxspeed`, road class | fetched **once** into `fixtures/`, never called live |
| **Sun position** | the sunset KPI | computed locally, NOAA formula, ~20 lines, no network |

The sun calculation is worth the effort: knowing the sun's azimuth and elevation
at the planned start time lets us find roads that *face* a sunset during golden
hour. It costs almost nothing and produces the single most memorable line in the
demo.

---

## 7. Stage ⑤ — Score every square from 0 to 1

Four composite scores per square. Every input is normalised **per kilometre**
and percentile-scaled against the crowd, so all terms are comparable and
dimensionless.

### Scenic — time-dependent, `f(t)`

Curviness · road type (penalise motorway and inner city) · forest · water ·
altitude and elevation gain · `maxspeed` · **sunset window** · **lack of
traffic**.

### Fun

Curviness · `leaned_m / path_m` (share of distance actually leaned over) ·
`band_share` (the brief's 50–120 km/h green flag) · flow, as
`1 − crawl_share` (the "standstills" red flag) · `rpm_mean`.

### Risk

Rain or snow · temperature below 8 °C (a red flag in the brief) · ABS rate per
km · hard-decel rate · congestion at that hour · capability gap (below).

### Growth — the differentiator

| Term | Definition |
|---|---|
| **Lean stretch** | reward squares whose crowd `lean_p50` sits just **above** the rider's p90 and **below** their p99 — a stretch, never a cliff |
| **New terrain** | the square's level-14 prefix is not in the rider's visited set (this is also what draws the fog map) |
| **New conditions** | a weather band the rider has not ridden in — only when risk allows |
| **Records in reach** | proximity to their personal best max-lean, max-altitude, longest ride |

Growth is *always* clipped by the safety constraint. That is the whiteboard's
*"push riders to a safe limit so he learns while being safe"*, expressed as
arithmetic rather than a slogan.

### Why we do not use BMW's own `funFactor`

The sample viewer ships an experimental formula (`app.js:203`) that multiplies by
`pathKm` — so a square scores higher **simply for containing more road**,
regardless of quality. It is an exposure count hiding inside a quality score.

We normalise per kilometre instead, and use `n_trips` only as a **confidence
weight**: few riders → shrink the score toward the regional mean. Saying this
out loud on stage is the strongest available signal that we actually read their
data rather than skimming it.

---

## 8. Stage ⑥ — The cost function

For each edge `e` at planned time `t`, with length `L` km and observed speed `v`:

```
value(e,t) = w_scenic·scenic(e,t) + w_fun·fun(e) + w_growth·growth(e,R)
risk(e,t)  = w_abs·abs_rate + w_dec·hard_decel + w_wx·weather(t) + w_tr·congestion(e,t)
time(e,t)  = L / v(e, band(t))

cost(e,t)  = time(e,t) · ( 1 + α·(1 − value(e,t)) + β·risk(e,t) )

EXCLUDE e  if  crowd_lean_p95(e) − rider_lean_p95(R) > margin
```

Four properties that matter:

1. **Every term is positive.** `time > 0`, and the multiplier is ≥ 1. So there
   are no negative-weight edges and Dijkstra is provably correct. No
   Bellman-Ford, no surprises.
2. **`α` is the single user-facing dial** — the *Chill ↔ Sportive* slider. It is
   literally "how much extra time will you accept for a better road".
3. **Safety is a hard exclusion, not a penalty.** A rider whose lean tops out at
   20° is not *discouraged* from a road the crowd rides at 45° — that road is
   removed from their graph entirely. A soft penalty can always be overwhelmed
   by a large enough fun bonus; an exclusion cannot.
4. **Speed has a documented fallback chain:** square median → level-14 band
   median → regional median. Never let a missing `v` produce a divide-by-zero
   shortcut.

### A worked example

Munich → Kesselberg. One 100 m hop on each of the two candidate roads, with
β = 1:

| | A95 motorway | Kesselberg pass |
|---|---|---|
| Observed speed | 120 km/h | 50 km/h |
| Real time | 3.0 s | 7.2 s |
| `value` | 0.10 | 0.90 |
| `risk` | 0.05 | 0.20 |

**At `α = 0`** (pure speed):
`motorway = 3.0 × 1.05 = 3.2` vs `pass = 7.2 × 1.20 = 8.6` → **motorway wins.**

**At `α = 3`** (sportive):
`motorway = 3.0 × (1 + 3(0.90) + 0.05) = 3.0 × 3.75 = 11.3`
`pass     = 7.2 × (1 + 3(0.10) + 0.20) = 7.2 × 1.50 = 10.8` → **the pass wins.**

Same algorithm, same data, one slider. **This inversion is our primary
correctness test** — if it does not flip, the cost function is wrong.

---

## 9. Stage ⑦ — Search

### Mode 1: Destination (A → B)

Plain Dijkstra with a binary heap. Snap origin and destination to the nearest
square that has data.

```
dijkstra(graph, start, goal, cost_fn):
    dist[start] = 0;  heap = [(0, start)]
    while heap:
        d, u = pop_min(heap)
        if u == goal: return reconstruct(u)
        if d > dist[u]: continue            # stale entry
        for (v, edge) in neighbours(u):
            if excluded(edge, rider): continue
            nd = d + cost_fn(edge, t)
            if nd < dist[v]: dist[v] = nd; prev[v] = u; push(heap, (nd, v))
```

~300k nodes and ~1M edges resolves in well under a second in pure Python, so no
scipy dependency is required.

**Three route options** — run the search at three values of `α` (Chill /
Balanced / Full Send) and return all three. They are genuinely different routes,
not cosmetic variants, because `α` reweights every edge.

**Time budget** — "I have 90 minutes to get to Tegernsee" is a binary search on
`α`: higher `α` means a longer, better route, so search `α ∈ [0, α_max]` until
the route duration lands within ±10% of the budget. Six or seven iterations.

Note that duration versus `α` **steps** rather than curving smoothly, because
paths are discrete. Treat monotonicity as a smoke test only — the binary search
still converges, but do not assert strict monotonicity.

### Mode 2: Joyride (X hours from here, return home)

The same Dijkstra, called four times. No second engine.

1. **Flood outward** from the origin once → travel time to every reachable
   square.
2. **Take the ring** of squares at ≈ T/2 travel time.
3. **Pick top-K turnarounds, spread across bearings.** Bearing diversity is what
   makes the three offered loops look and feel genuinely different on the map
   rather than three variations on the same valley.
4. For each candidate: route **out** on the fun-weighted cost, then route
   **back** with a reuse penalty on already-traversed edges — which is precisely
   BMW's own `alreadyUsedRoads="allow|forbid"` route option, so we are speaking
   their vocabulary.
5. **Rank complete loops** by value per hour; return the best three.

### Sunset scheduling

Because `scenic` is `f(t)`, the planner can *place* a west-facing leg inside the
golden-hour window rather than merely reporting when sunset is:

> *"Leave at 18:10 and you'll be at the Kesselberg overlook facing west at
> sunset."*

One formula, no extra dependency, and the most memorable moment in the demo.

---

## 10. Stage ⑧ — How the algorithm explains itself

The brief states that *"explainability + live demo will score bonus points"*, and
it asks for "an overview of all used data sources and how they are weighted".
So explanation is a first-class output, not a slide. Every route response
carries the reasoning that produced it.

**1. Your profile, as a bar chart.**
*"We think you like curves and altitude — across your 101 rides you ride 1.8×
curvier roads than the average BMW rider."* Sourced, not asserted. Switching
rider in the header re-profiles the entire dashboard live.

**2. Per-route KPI bars.**
Fun, scenic, safety and growth, plus km, minutes, curviness in °/km, elevation
gain, and % of time in the 50–120 km/h band.

**3. The map coloured by *why*.**
Each stretch tinted by whichever KPI earned its place. You can see at a glance
that this section is here for the corners, this one for the lake, and this one is
just the connection out of town.

**4. The road-not-taken panel.**
Show the fast route beside the chosen one and decompose the difference:

> *"14 minutes slower. 3.2× the lean changes. 340 m more climb. One fewer
> inner-city crossing."*

This is the sentence that wins the room, because it states a **trade-off
honestly** instead of asserting a score.

**5. The ride preview.**
Play the route before riding it, with lean-angle and speed gauges moving to
**crowd-predicted** values. BMW's sample viewer already implements playback with
exactly those gauges — we feed it a planned route instead of a recorded one.
See [STACK.md](STACK.md) for the shared `Track` shape that makes this
nearly free.

**6. Honest gaps.**
Where there is no crowd data, the UI says so rather than scoring zero and
quietly routing around a perfectly good road. Confidence shrinkage
(`n_trips` → toward regional mean) is surfaced, not hidden.

---

## 11. Complexity and scalability

| Stage | Cost | Notes |
|---|---|---|
| ① Aggregation | one pass over 13 GB | DuckDB, spills to disk; ~6 GB RAM free on the dev box |
| ① Output | ~30 MB Parquet | **13 GB → 30 MB.** This reduction *is* the scalability answer |
| ② Edge build | O(points) | one pass over the same aggregate |
| ③ Profile | O(rider points) | cached per rider |
| ⑦ Dijkstra | O(E log V) | ~1M edges → well under a second |
| Spatial query | prefix match | `morton_code LIKE '…%'` — no spatial index to maintain |

The argument generalises unchanged: cells are independent, so aggregation is
embarrassingly parallel and shards by morton prefix. Scaling from Bavaria to all
of Germany is more machines on the offline pass, with the same serving cost.

---

## 12. Known limitations — state these before a judge finds them

- **Coverage is Bavarian.** The crowd data concentrates at 47.6–48.5 N,
  11.0–11.9 E. We route where BMW has riders. Own it: *"BMW's crowd data is
  Bavarian, so that's where we demo."*
- **The graph cannot route where nobody has ridden.** A genuinely great road with
  no BMW traffic is invisible to us. This is a real trade-off for the benefit of
  observed-speed and observed-lean data, and OSM enrichment is the documented
  path out of it.
- **Curviness conflates road and rider.** A cautious rider on a twisty road logs
  less lean change than a fast one. Aggregating across many riders and using
  `n_trips` as a confidence weight mitigates this; it does not eliminate it.
- **Traffic is a historical prior, not live.** An accident today is invisible.
  Stated plainly, this is a deliberate trade for demo reliability.
- **Road construction is not modelled** unless it is in the cached OSM extract.
- **Sunset assumes an unobstructed western horizon.** We know road bearing and
  terrain elevation, not tree lines.

---

## 13. Verification checklist

| Gate | Test |
|---|---|
| **Morton encoder** | Encode 1,000 raw rows and assert the first 18 base-4 digits equal that row's own `morton_code` prefix. Exact ground truth, instant. **Run before the 13 GB pass** — a wrong encoder invalidates everything. |
| Aggregation sanity | `curviness` shows a fat near-zero mode (motorways) and a right tail (passes); no square with `path_m` ≤ 50 survives; level-14 temporal buckets are mostly **non-empty** |
| Edge sanity | no edge > 150 m; every edge ≥ 2 trips; eyeball the 20 longest on a map |
| **Cost inversion** | Munich→Kesselberg takes the pass at high `α` and the A95 at `α = 0` |
| Time budget | binary search lands within ±10% of the requested duration |
| Safety constraint | a synthetic low-lean rider has the Kesselberg squares **excluded**, not merely penalised |
| Profiles differ | riders A, B and C produce three different weight vectors — if identical, the z-scoring is broken |
| Joyride | returns to origin; outbound and return legs substantially non-overlapping; three loops differ in bearing |

Do not try to validate cell alignment by loading `cells.parquet` into BMW's
tripViewer — it expects the 42-column trip schema, not an aggregate. Use the
`morton_code` column comparison above instead.
