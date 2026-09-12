# The Route Algorithm

How BMW Ride Planner decides what a good ride is, how it adapts to the rider,
how it helps them improve, and how it proves all of that on screen.

Grounded in columns that were **measured** in BMW's dataset, not assumed — see
[DATASET.md](DATASET.md) for fill rates and dead columns, and
[DATA_SOURCES.md](DATA_SOURCES.md) for the external data layer. Read both before
implementing any of this.

---

## 1. The core idea in one paragraph

Ordinary navigation asks *"which way is fastest?"* and adds up minutes. We use
the same search algorithm — Dijkstra — but we **distort the perceived cost of
each road in a controlled, explainable way**. A boring road is told it is longer
than it looks; a great road is told it is shorter. Then we simply ask for the
cheapest path, and what comes back is a good *ride* rather than a fast *trip*.

Everything below is (a) deciding how much to distort, (b) making that distortion
personal to the rider, (c) adding a measured dose of challenge so the rider
actually improves, and (d) being able to show our working afterwards.

The distortion never makes a road *free* or *negative*. That keeps Dijkstra
provably correct, which matters because we have to defend it on stage.

---

## 2. Pipeline overview

```
  BMW crowd lake                BMW personal trips
  85,699 trips / 13 GB          rider A / B / C
        │                              │
        │ ① aggregate                  │ ⑤ profile: taste · capability · context
        ▼                              │ ⑥ skill vector  ──►  learning
   cells.parquet                       │
        │                              │
        │ ② graph: observed transitions (+ OSM topology)
        ▼                              │
   edges.parquet                       │
        │                              │
        │ ③ static enrichment          │
        │   OSM · DEM · land cover · accident rates
        ▼                              │
   cells_enriched.parquet              │
        │                              │
        └──────────┬───────────────────┘
                   │  ④ dynamic context: weather · live traffic
                   ▼
             ⑦ scoring: scenic · fun · risk · growth
                   ▼
             ⑧ cost function
                   ▼
             ⑨ Dijkstra  ──►  Destination  /  Joyride
                   ▼
             ⑩ explanation payload  →  UI
                   │
                   └──►  ride happens  ──►  skill vector updates   ⟲
```

Stages ①–③ run **once, offline**. Stages ④–⑩ run **per request**, in well under
a second. The loop back from a completed ride into the skill vector is what
makes this a companion rather than a search engine.

---

## 3. Stage ① — Chop the map into squares and ask the crowd

### Why squares

BMW's dataset already ships a spatial index: every telemetry row carries a
`morton_code`, a 32-digit base-4 quadkey — effectively a postcode for a square
on the map, with one very useful property: **nearby squares share a code
prefix**. So:

- "everything near here" is a string prefix match (`morton_code LIKE '1220013%'`)
- zooming out is *truncating the string* — no re-aggregation needed

We aggregate at **level 18 ≈ 100 m** for road-quality features, and roll up to
**level 14 ≈ 1.6 km** for anything needing more samples per bucket.

The encoder is written and documented in BMW's own sample viewer at
`tripViewer/tripViewer/app.js:225`. **Port it verbatim.** It is an
equirectangular grid — `xf = (lon+180)/360`, `yf = (lat+90)/360`, both axes
divided by 360 — **not** Web Mercator. Rolling your own misplaces every cell.

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
| `abs_events` | ABS activation count | `ridingabsbraking` |
| `hard_decel` | samples under −3 m/s² | `sensorsaccelerationlongitudinal` |
| `elev_*` | elevation min / mean / max | `positionrawelevation` |
| `rpm_mean` | engine speed | `ridingenginespeed` |
| `temp_mean` | ambient | `sensorsoutsidetemperature` |
| `n_trips` | distinct rides through this square | **the filename** |
| `speed_by_band` | median speed × weekday/weekend × 3 time bands, at level 14 | `ridingvehiclespeed` |

### Curviness — the key measure, and why it is lean-based

Curviness is **degrees of lean change per kilometre**:

```
curviness = lean_delta / (path_m / 1000)
```

BMW's own definition (`app.js:188`), and cleverer than it looks. A long
constant-radius sweeper holds a near-constant lean angle and accumulates almost
no *change*. A proper series of corners swings left–right–left and racks up
change fast. So this measures **the thing riders actually enjoy** — direction
changes — rather than mere road geometry.

Steps below 2° are discarded as sensor noise, or every straight accumulates a
baseline from vibration.

> **This is the moat.** `sensorsbankingangle` is 100% filled in the crowd data,
> and phones cannot measure it. Every competing planner infers curvature from
> map geometry; we read it off the motorcycle.

### Implementation note that will bite you

The aggregation is one DuckDB query. Δlean needs a window function:

```sql
lag(sensorsbankingangle) OVER (PARTITION BY filename ORDER BY timestampinmillis)
```

**Partition by `filename`, never by `trip_id`.** `trip_id` is only 56% filled in
the lake — partitioning on it drops ~44% of rows into one null partition and
silently corrupts every Δlean across trip boundaries. Use
`read_csv('.../**/*.csv', filename=true)`.

### Traffic, measured instead of guessed

No free keyless real-time traffic source exists, and a key provisioned the night
before a demo is a liability. So congestion comes from **BMW's own data**:

```
congestion(cell, t) = 1 − ( speed_median(cell, band(t)) / speed_p85(cell) )
```

Bucket coarsely or it will be empty. 85,699 trips × ~310 median rows ≈ 27M
points over ~10⁵–10⁶ level-18 squares is only a few dozen points per square;
split across 24 h × 7 days it is mostly null. So compute the temporal prior at
**level 14** with **weekday/weekend × 3 time bands**. Morton rollup is string
truncation, so this is one extra `GROUP BY` in the same query.

A live traffic layer can override this per request where a key is configured —
see [DATA_SOURCES.md](DATA_SOURCES.md) §5 — but the prior is always available and
is the default.

---

## 4. Stage ② — The road network

**Fallback (build this first):** nodes are squares riders have actually ridden;
edges are **observed square→square transitions** from the crowd, carrying
observed median speed. No download, no external dependency, and it routes on
roads riders actually ride at speeds they actually ride them.

**Upgrade (once OSM is parsed):** take topology from the OSM extract — every
road, correctly connected, with one-ways and turn restrictions — and keep the
crowd data as the **quality layer** on top. This removes the fallback's real
limitation: it cannot route where nobody has ridden.

Build the crowd graph first because nothing blocks it; layer OSM in when the
extract is ready.

### Two filters that are not optional

**1. Drop transitions longer than ~150 m** (1.5× the level-18 cell diagonal).

If a rider's GPS drops out for 30 seconds, the data shows one "transition"
spanning two to five kilometres. Dijkstra will find it and love it — a free
shortcut with a plausible `time = L/v`. BMW's viewer guards against exactly this
at `app.js:176` with `MAX_GAP_M`. Without the filter, the Munich→Kesselberg test
can pass while the route quietly teleports.

**2. Require ≥ 2 distinct trips per edge.**

One rider's stray GPS sample should not create a road. This also prunes
*parallel-road collapse* — a motorway and its frontage road can fall inside one
100 m square, and only transitions riders actually made survive.

**Verify before trusting the graph:** assert no edge exceeds the threshold,
assert every edge has ≥ 2 trips, then **render the 20 longest edges and look at
them**. A teleport that survives into the demo is worse than a missing feature.

---

## 5. Stage ③ — Static enrichment, and scoring roads with no crowd data

Offline, `cells.parquet` is joined against the static external sources into
`cells_enriched.parquet`. Full detail in [DATA_SOURCES.md](DATA_SOURCES.md); the
features that matter to the algorithm:

| From | Features |
|---|---|
| **OSM** | `curvature_geo`, `junction_density`, `road_class`, `surface_quality`, `maxspeed`, `tunnel_share`, `viewpoint`, `water_prox`, `forest_share`, `construction` |
| **Copernicus DEM** | `gradient`, `elev_gain_per_km`, `relief`, `ridge_score`, `horizon_west` |
| **CLMS land cover** | `forest_frac`, `natural_frac`, `urban_frac`, `water_frac` |
| **Accident data** | `accident_rate` per million rider-km, split wet / dry / dark |

### The confidence blend

`curvature_geo` — heading change per km from OSM way geometry — measures the
same physical property as lean-derived `curviness`, but **without needing any
rider to have been there**. That lets us degrade gracefully instead of going
blind:

```
c(cell)       = n_trips / (n_trips + k)             # k ≈ 5; confidence in crowd data
quality(cell) = c · crowd_score + (1 − c) · geometry_score
```

A well-ridden road uses **measured lean**. An unridden road falls back to
**geometry and terrain**. The UI surfaces `c` so a judge can see which is which.

This is also the principled fix to BMW's own experimental `funFactor`
(`app.js:203`), which multiplies by `pathKm` — so a square scores higher simply
for *containing more road*, regardless of quality. That is an exposure count
hiding inside a quality score. We normalise per kilometre and use trip count
**only** as confidence, never as a fun multiplier. Say this out loud on stage:
it is the strongest available signal that we actually read their data.

### Accident rate must be exposure-normalised

Raw accident counts would penalise exactly the roads riders love, because famous
motorcycling roads carry far more motorcycle traffic. The crowd data gives us
the denominator for free:

```
exposure(cell)      = n_trips(cell) × path_m(cell)          # rider-metres
accident_rate(cell) = motorcycle_accidents(cell) / exposure(cell)
```

A road is now flagged only if it is dangerous **relative to how much it is
ridden**. That is the difference between a statistic and an insight.

---

## 6. Stage ④ — The rider profile, and the three ways it changes the route

This is where "personalisation" stops being a slogan. The profile influences the
route through **three separate channels**, and keeping them separate is what
makes the system explainable *and* safe.

| Channel | Question | Mechanism | Overridable? |
|---|---|---|---|
| **Taste** | what does this rider enjoy? | KPI **weights** | Yes — profile chips and the `α` slider |
| **Capability** | what can they handle safely? | **hard exclusions** and ceilings | **No. Never** |
| **Context** | when and how do they ride? | defaults, suggestions, notifications | Yes |

### Channel 1 — Taste: revealed-preference weights

Take the squares the rider has actually ridden, distance-weight them, and
compare each feature's mean against the crowd baseline:

```
z_R(f) = ( μ_R(f) − μ_crowd(f) ) / σ_crowd(f)      # per feature f
w_R(f) = normalise( clip(z_R(f), 0, ∞) )           # weights, Σw = 1
```

In words: *whatever this rider consistently over-indexes on becomes a heavier
weight in their personal scoring.* If their rides are 1.8× curvier and 300 m
higher than the average BMW rider, curviness and altitude dominate their weights.

About thirty lines of code, no training, no black box — and the weight vector
goes **straight onto a bar chart in the UI**. That is the whole reason to prefer
it to a fitted model: the brief promises bonus points for explainability, and a
judge can audit this on screen in five seconds.

The **Sportive / Safer / Chill** chips are priors on these weights. Personal data
moves them away from the chosen prior; with no personal data yet, the prior is
all you get. So a brand-new rider still gets a sensible route, and the system
gets more personal with every ride — which is itself a slide.

### Channel 2 — Capability: the style ratio, and why raw lean is not enough

Naively, rider capability is their p95 lean angle. That is **wrong in a way that
matters**: lean angle conflates the road with the rider. A fast rider on a
motorway logs less lean than a cautious rider on a mountain pass. Comparing raw
lean across riders compares the roads they happen to live near.

The fix is to normalise by the road. For every square the rider shares with the
crowd:

```
style_ratio(R) = median over shared cells (  rider_lean_p95(cell)
                                           / crowd_lean_p95(cell)  )
```

Now the number means something: **0.6 = rides well within the road's demands;
1.2 = rides harder than the crowd on the same tarmac.** Road-normalised, and
comparable between riders who never ride the same roads.

From this we derive the ceiling used for exclusions:

```
lean_ceiling(R) = crowd_lean_p95 × style_ratio(R) × safety_margin
EXCLUDE cell if crowd_lean_p95(cell) > lean_ceiling(R)
```

**Capability is a hard exclusion, not a penalty.** A soft penalty can always be
overwhelmed by a large enough fun bonus — precisely the failure mode you cannot
ship in a motorcycle product. An exclusion cannot be outvoted.

Also derived here: speed envelope relative to `maxspeed`, and braking behaviour
from `ridingabsbraking` + `sensorsaccelerationlongitudinal` (**not** brake
pressure — that column is 0% filled everywhere).

### Channel 3 — Context

- **Free-time window** — histogram of trip start times, weekday × hour, plus
  typical duration. Seeds the time budget and drives the Overview greeting.
  *"You usually ride Saturday 09:00–13:00 — next window is dry and 19 °C."*
- **Weather history** — from the Open-Meteo **archive**, every past trip
  retroactively labelled with the conditions it happened in. Feeds the
  conditions dimension of learning.
- **Bike class** — inferred from rpm-per-km/h, gear count, lean envelope, tyre
  pressure, and **confirmable by the rider in the UI**, because bike model is
  not in the dataset.
- **Novelty appetite** — how often they repeat roads versus ride new ones,
  measured from their own history. Sets the default balance between *revisit a
  favourite* and *explore something new*.

### Three things we must not claim

The dataset contains no rider **age**, no **bike model**, and no **road
construction**. All three appear in our whiteboard notes. "Usage of BMW Personal
Rider Data" is graded, and claiming a field we do not have is the one thing that
reads as fabrication to a BMW engineer. Profiles are behavioural only.

Also: `sensorsaccelerationlateral` is 70% filled on crowd data but **3% and 5%
for two of the three example riders**. It must never enter the personal profile —
it would pass every test against rider C and silently return nothing for two
real riders.

---

## 7. Stage ⑤ — The learning system

The whiteboard note is *"push riders to a safe limit so he learns while being
safe."* This section is that sentence turned into arithmetic. It is the
differentiator, so it is worth getting right.

### 7.1 The skill vector

The rider's demonstrated level across five independent dimensions:

| Dimension | Measure | Source |
|---|---|---|
| **Lean** | p90 of \|lean\|, plus `style_ratio` | `sensorsbankingangle` |
| **Curviness** | p90 of the curviness of squares ridden | crowd aggregate ∩ their trips |
| **Gradient** | p90 of gradient ridden, and max altitude | Copernicus DEM |
| **Conditions** | set of weather bands ridden in (rain, cold, wind) | Open-Meteo archive |
| **Terrain** | set of level-14 squares visited; % of region covered | morton prefixes |

Each dimension carries three numbers:

```
current_d   = rider p90 on dimension d          # what they have demonstrably done
ceiling_d   = safe maximum for this rider       # from capability, never crossed
Δ_d         = stretch increment                 # small, e.g. 10% of current
```

`current` uses **p90 rather than max** deliberately. A single lurid lean angle
from a near-miss or a kerb strike is not a demonstrated capability, and building
progression off a maximum would ratchet the rider upward off one bad data point.

### 7.2 What makes a square a growth opportunity

A square is a **stretch** on dimension `d` if its demand sits just above what the
rider has done, and still below their ceiling:

```
stretch_d(cell, R) = 1   if  current_d(R) < level_d(cell) ≤ current_d(R) + Δ_d
                     0   otherwise
```

Then two gates, and the second is the important one:

```
GATE 1 (safety):   level_d(cell) ≤ ceiling_d(R)  for every dimension d
GATE 2 (novelty):  |{ d : level_d(cell) > current_d(R) }| ≤ 1
```

**Gate 2 — one novelty at a time — is the core safety idea of the whole
feature.** A road that is slightly curvier than usual, in familiar weather, on a
familiar road type, is a good place to learn. A road that is curvier *and*
steeper *and* wet is not — even though each factor individually sits inside the
stretch band. Compound novelty is how riders get hurt, and it is exactly what a
naive "maximise growth" objective would select for.

It also matches how skills coaching works in any physical discipline, which
makes it easy to defend in the room.

### 7.3 The challenge dose — a route-level budget

Progressive overload is not "ride at your limit all day". A learning route should
be **mostly comfortable, with a measured dose of stretch**:

```
dose(route) = Σ length(e) over edges where stretch(e) = 1
              ────────────────────────────────────────────
                        total route length

target dose:   Chill 5%   ·   Balanced 10–15%   ·   Sportive 20–25%
```

This is a **route-level constraint, not an edge cost** — Dijkstra cannot express
it directly, exactly like the time budget. Same solution: generate candidate
routes across a range of growth weights, measure the actual dose of each, and
return the candidate whose dose lands in the target band. Six or seven
candidates is plenty.

Keeping the dose an explicit, displayed number is also good product design: the
rider sees *"18% of this ride is new ground for you"* rather than trusting an
opaque "challenge level".

### 7.4 Weather shrinks the ceiling — one mechanism, two problems solved

```
ceiling_d(R, t) = ceiling_d(R) × weather_factor(t)
weather_factor  = 1.0 dry  ·  ~0.7 wet  ·  ~0.5 cold + wet  ·  0 snow/ice
```

Because ceilings gate the stretch band, a wet forecast automatically collapses
the growth opportunities to zero and the router falls back to comfortable roads.
**No separate rule is needed for "don't teach someone a new lean angle in the
rain"** — it falls out of the same arithmetic. That elegance is worth pointing
out on stage.

The accident data adds a second, independent veto: **never set a growth target on
a road with an elevated motorcycle-accident rate**, however well it matches the
stretch band. Learning happens on safe roads.

### 7.5 The feedback loop — how learning actually closes

This is what makes it a learning system rather than a recommendation:

1. **Propose** — the route includes a measured dose of stretch squares.
2. **Observe** — the rider rides it. New telemetry arrives.
3. **Compare** — on the stretch squares specifically, did they ride at the
   stretch level?

```
realised(cell) = rider_lean_p95_on_this_ride(cell) / crowd_lean_p95(cell)

realised ≥ style_ratio            → they met it.      current_d advances.
realised < style_ratio × 0.85     → they backed off.  current_d holds,
                                    and the next dose is reduced.
```

4. **Update** — recompute the skill vector, coverage set and records.

The whole loop runs on telemetry alone. **We never have to ask the rider how it
went** — the bike already told us. *"You took the Kesselberg at 0.8× your usual
lean. We won't push that one again yet."* That sentence, delivered from data the
rider never entered, is the demo moment that sells the thesis.

### 7.6 What the rider sees

| Surface | Content |
|---|---|
| **Learnings** | Progression per dimension over time; "your next challenge" as a concrete named road, with the safety reasoning shown |
| **Records** | New max lean, max altitude, longest ride, first wet ride, most curvature in one ride — all falling out of the skill vector |
| **Explore (fog map)** | Level-14 squares visited vs the region; *"you've explored 18% of Bavaria"*; unexplored **fun-dense** areas glowing as targets |
| **Route badges** | `Stretch your lean` · `New terrain` · `New conditions` on suggestion cards |

The fog map is not decoration — it is the Terrain dimension of the skill vector,
rendered. That is why it belongs in the algorithm doc and not only in the UI doc.

---

## 8. Stage ⑥/⑦ — Dynamic context and scoring

Dynamic per request: **weather** (Open-Meteo forecast, sampled along the route at
the hour the rider will actually be there — not one value for the whole ride) and
**live traffic** where configured, else the crowd prior.

Four composite scores per square. Every input is normalised **per kilometre** and
percentile-scaled against the crowd, so all terms are comparable and
dimensionless.

| Score | Inputs |
|---|---|
| **Scenic** `f(t)` | curviness · `forest_frac` · `water_prox` · `viewpoint` · `relief` · `ridge_score` · `elev_gain_per_km` · −`urban_frac` · −`tunnel_share` · −motorway penalty · **sunset window** · −congestion |
| **Fun** | curviness (crowd) blended with `curvature_geo` by confidence · `leaned_m/path_m` · `band_share` (the 50–120 km/h green flag) · `gradient` · flow as `1 − crawl_share` · −`junction_density` · `rpm_mean` |
| **Risk** | `accident_rate` (condition-matched) · `abs_events`/km · `hard_decel`/km · `surface_quality` · `precip` · `temp < 8 °C` · `wind_gust` · `visibility` · congestion · capability gap |
| **Growth** | `stretch` on any dimension, subject to both gates (§7.2) |

### The sunset window

Sun azimuth and elevation are computed locally (NOAA formula, ~20 lines, no API).
But azimuth alone only says *where the sun is* — not whether the rider can see
it. Sampling the DEM along that bearing gives `horizon_west`: is the view toward
the sun actually open, or is there a mountain in the way.

That turns a plausible-sounding feature into an honest one, and it is what makes
the joyride showpiece defensible rather than decorative.

---

## 9. Stage ⑧ — The cost function

For each edge `e` at planned time `t`, with length `L` km and observed speed `v`:

```
value(e,t) = w_scenic·scenic(e,t) + w_fun·fun(e) + w_growth·growth(e,R)
risk(e,t)  = w_acc·accident_rate + w_abs·abs_rate + w_dec·hard_decel
             + w_wx·weather(t) + w_tr·congestion(e,t) + w_surf·surface
time(e,t)  = L / v(e, band(t))

cost(e,t)  = time(e,t) · ( 1 + α·(1 − value(e,t)) + β·risk(e,t) )

EXCLUDE e if:  crowd_lean_p95(e) > lean_ceiling(R, t)      # capability, weather-adjusted
               accident_rate(e) > severity_threshold        # accident outliers
               construction or closure on e                 # OSM / live traffic
               surface forbidden by rider options           # e.g. dirtRoads=forbid
               snow or ice at t
```

Four properties that matter:

1. **Every term is positive.** `time > 0` and the multiplier is ≥ 1, so there are
   no negative-weight edges and Dijkstra is provably correct. No Bellman-Ford, no
   surprises.
2. **`α` is the single user-facing dial** — the *Chill ↔ Sportive* slider,
   literally "how much extra time will you accept for a better road".
3. **Safety is exclusion, not penalty.** Unoutvotable by design.
4. **Speed has a documented fallback chain:** square median → level-14 band
   median → regional median. Never let a missing `v` produce a divide-by-zero
   shortcut.

### A worked example

Munich → Kesselberg. One 100 m hop on each candidate road, β = 1:

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

Same algorithm, same data, one slider. **This inversion is the primary
correctness test** — if it does not flip, the cost function is wrong.

---

## 10. Stage ⑨ — Search

### Mode 1: Destination (A → B)

Plain Dijkstra with a binary heap. Snap origin and destination to the nearest
square with data.

```
dijkstra(graph, start, goal, cost_fn):
    dist[start] = 0;  heap = [(0, start)]
    while heap:
        d, u = pop_min(heap)
        if u == goal: return reconstruct(u)
        if d > dist[u]: continue               # stale entry
        for (v, edge) in neighbours(u):
            if excluded(edge, rider, t): continue
            nd = d + cost_fn(edge, t)
            if nd < dist[v]: dist[v] = nd; prev[v] = u; push(heap, (nd, v))
```

~300k nodes and ~1M edges resolves in well under a second in pure Python, so no
scipy dependency is required.

**Three route options** — run at three values of `α` (Chill / Balanced / Full
Send). Genuinely different routes, not cosmetic variants, because `α` reweights
every edge.

**Time budget** — "I have 90 minutes" is a binary search on `α`: higher `α` means
a longer, better route, so search `α ∈ [0, α_max]` until duration lands within
±10% of the budget. Six or seven iterations.

**Challenge dose** — the same candidate-and-measure loop over the growth weight
(§7.3). Both budgets are route-level constraints resolved by generating
candidates and measuring, never by distorting an edge cost.

Note that duration versus `α` **steps** rather than curving smoothly, because
paths are discrete. Treat monotonicity as a smoke test only.

### Mode 2: Joyride (X hours from here, return home)

The same Dijkstra, called four times. No second engine.

1. **Flood outward** from the origin once → travel time to every reachable
   square.
2. **Take the ring** of squares at ≈ T/2 travel time.
3. **Pick top-K turnarounds, spread across bearings.** Bearing diversity is what
   makes the three offered loops look and feel genuinely different rather than
   three variations on the same valley. Bias candidate selection toward
   unexplored squares to serve the Terrain dimension.
4. Route **out** on the fun-weighted cost, then **back** with a reuse penalty on
   already-traversed edges — precisely BMW's own
   `alreadyUsedRoads="allow|forbid"` option, so we speak their vocabulary.
5. **Rank complete loops** by value per hour; return the best three.

### Sunset scheduling

Because `scenic` is `f(t)`, the planner can *place* a west-facing leg inside the
golden-hour window rather than merely reporting when sunset is:

> *"Leave at 18:10 and you'll be at the Kesselberg overlook facing west at
> sunset."*

One formula plus one DEM lookup, and the most memorable moment in the demo.

---

## 11. Stage ⑩ — How the algorithm explains itself

The brief states *"explainability + live demo will score bonus points"*, and asks
for "an overview of all used data sources and how they are weighted". So
explanation is a first-class output, not a slide. Every route response carries
the reasoning that produced it.

**1. Your profile, as a bar chart.** *"We think you like curves and altitude —
across your 101 rides you ride 1.8× curvier roads than the average BMW rider."*
Sourced, not asserted. Switching rider in the header re-profiles the entire
dashboard live.

**2. Per-route KPI bars.** Fun, scenic, safety, growth, plus km, minutes,
curviness in °/km, elevation gain, and % of time in the 50–120 km/h band.

**3. The map coloured by *why*.** Each stretch tinted by whichever KPI earned its
place — corners here, the lake there, and this bit is just the connection out of
town.

**4. The road-not-taken panel.** The fast route beside the chosen one, with the
difference decomposed:

> *"14 minutes slower. 3.2× the lean changes. 340 m more climb. One fewer
> inner-city crossing."*

The sentence that wins the room, because it states a **trade-off honestly**
instead of asserting a score.

**5. The learning card.** *"18% of this ride is new ground for you: the
Kesselberg section asks for about 8° more lean than you've ridden, in dry weather
on a road type you know. Accident rate there is below the regional average for
the traffic it carries."* One dimension stretched, named; the gates shown; the
safety evidence cited.

**6. Ride preview.** Predicted lean and speed gauges moving along a road the
rider has not ridden yet.

**7. Honest gaps.** Where crowd data is thin the UI shows the confidence `c` and
says the score is geometry-derived, rather than scoring zero and quietly routing
around a perfectly good road.

---

## 12. Complexity and scalability

| Stage | Cost | Notes |
|---|---|---|
| ① Aggregation | one pass over 13 GB | DuckDB, spills to disk; ~6 GB RAM free on the dev box |
| ① Output | ~30 MB Parquet | **13 GB → 30 MB.** This reduction *is* the scalability answer |
| ② Edge build | O(points) | one pass over the aggregate |
| ③ Enrichment | one spatial join per source | offline, cached; DEM and land-cover sampling are raster lookups |
| ④ Profile + skill vector | O(rider points) | cached per rider, invalidated on new trips |
| ⑨ Dijkstra | O(E log V) | ~1M edges → well under a second |
| Spatial query | prefix match | `morton_code LIKE '…%'` — no spatial index to maintain |

The argument generalises unchanged: cells are independent, so aggregation is
embarrassingly parallel and shards by morton prefix. Scaling from Bavaria to all
of Germany is more machines on the offline pass, with identical serving cost.

---

## 13. Known limitations — state these before a judge finds them

- **Coverage is Bavarian.** The crowd lake concentrates at 47.6–48.5 N,
  11.0–11.9 E. Own it: *"BMW's crowd data is Bavarian, so that's where we
  demo."* With OSM topology and the confidence blend we can still route outside
  it, on geometry rather than measured lean.
- **Curviness conflates road and rider.** Mitigated by aggregating across many
  riders, by `n_trips` confidence weighting, and by `style_ratio` on the personal
  side — but not eliminated.
- **Traffic is a historical prior unless a key is configured.** An accident today
  is invisible without the live layer. A deliberate trade for demo reliability.
- **Accident data is historical and sparse** at 100 m resolution. Aggregate to
  level 14 before trusting a rate, and treat low-exposure squares as unknown
  rather than safe.
- **Sunset assumes the DEM tells the whole story.** We model terrain horizon, not
  tree lines or buildings.
- **The learning loop needs rides to close.** With three example riders and a
  fixed dataset we can *demonstrate* the update arithmetic on their history, but
  we cannot show months of progression. Be explicit that the loop is shown
  retrospectively on real data, not simulated forward.
- **`style_ratio` needs shared squares.** A rider with no overlap with the crowd
  has no road-normalised ratio; fall back to the Safer prior and say so.

---

## 14. Verification checklist

| Gate | Test |
|---|---|
| **Morton encoder** | Encode 1,000 raw rows; assert the first 18 base-4 digits equal that row's own `morton_code` prefix. Exact ground truth, instant. **Run before the 13 GB pass** — a wrong encoder invalidates everything |
| Aggregation sanity | `curviness` shows a fat near-zero mode (motorways) and a right tail (passes); no square with `path_m` ≤ 50 survives; level-14 temporal buckets mostly **non-empty** |
| Edge sanity | no edge > 150 m; every edge ≥ 2 trips; eyeball the 20 longest on a map |
| **Cost inversion** | Munich→Kesselberg takes the pass at high `α`, the A95 at `α = 0` |
| Confidence blend | a square with `n_trips = 0` still receives a fun score, from geometry; `c` reported as ~0 |
| Accident normalisation | a popular fun road does **not** outrank a quiet dangerous one on `accident_rate` — if it does, the exposure denominator is wrong |
| Time budget | binary search lands within ±10% of the requested duration |
| Challenge dose | measured dose lands in the profile's target band; `dose = 0` on a wet forecast |
| **Gate 2 (novelty)** | construct a square that is a stretch on lean *and* gradient *and* conditions; assert it is **not** offered as growth |
| Ceiling shrink | same request, dry vs wet forecast → wet excludes the stretch squares |
| Safety exclusion | a synthetic low-`style_ratio` rider has the Kesselberg squares **excluded**, not merely penalised |
| Feedback loop | replay a rider's trips chronologically; assert `current_lean` is monotone non-decreasing and never advances on a ride where `realised < style_ratio × 0.85` |
| Profiles differ | riders A, B and C produce three different weight vectors *and* three different style ratios — if identical, the z-scoring is broken |
| Joyride | returns to origin; legs substantially non-overlapping; three loops differ in bearing |

Do not try to validate cell alignment by loading `cells.parquet` into BMW's
tripViewer — it expects the 42-column trip schema, not an aggregate. Use the
`morton_code` comparison above.
