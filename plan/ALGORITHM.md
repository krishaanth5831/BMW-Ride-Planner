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

## 2. The unit of analysis: road segments, not a grid

Everything is scored on **real road segments**. This is deliberate and it is the
foundation for everything else.

```
  Named road / pass          "Kesselberg (B11)"        → suggestions, UI labels
        │
  Road segment               junction → junction,      → THE ROUTABLE UNIT.
    (uniform ~200 m           subdivided evenly           all four scores live here
     chainage)
        │
  Corner / straight events   from the lean trace       → flow, rhythm,
    (optional layer)                                      per-corner capability
```

**Junctions are nodes. Segments between them are edges.** The scoring unit and
the routing edge are the same object, so routes follow real roads — no chains of
grid-cell centroids needing smoothing.

### Why not a grid

A 100 m square is not a road. It can contain a motorway and its frontage road,
or a junction, or half a corner. Worse, **a hairpin straddling a cell boundary
is measured as two half-corners**, each with diluted curviness — degrading the
single best feature we have because of an arbitrary line on a map. And "flow" —
a green flag in BMW's brief — is a property of a *sequence* of corners, which
binning destroys outright.

Segments also have **better statistics** than cells, because one road's data
stops being split across several squares.

### What morton codes are still for

`morton_code` remains an excellent **spatial index** and a poor **unit of
analysis**. Keep it for:

- prefix-match "everything near here" queries (`morton_code LIKE '1220013%'`)
- bucketing candidate segments during the snapping join, which is what makes
  that join fast
- coarse rider coverage

It is no longer what we score.

---

## 3. Stage ① — Snap the crowd onto segments

### Building segments

From the OSM extract: take road ways of rideable classes, **split them at every
junction node** (any node where ways meet, i.e. degree ≠ 2), then **subdivide
long segments to uniform ~200 m chainage**.

Uniform chainage matters. Raw junction-to-junction ways vary from 30 m stubs to
2 km runs; a 2 km segment dilutes one great corner into an average, and a 30 m
stub is statistical noise. Uniform length restores the comparability that made a
grid attractive, without the arbitrary boundaries.

**Fallback with no OSM:** build the crowd-transition graph, then **collapse every
chain of degree-2 nodes into a single polyline**. That is how routing graphs are
built from raw networks anyway — junctions are the nodes with degree ≠ 2, and
everything between them is one segment. You get road-following geometry with
zero downloads. Either way, **the unit is a segment, never a square.**

### Snapping — and why it is cheap here

BMW's `positionmapmatched*` coordinates are already projected onto road
centrelines: **measured mean offset from raw GPS is 16 m, with 62% of samples
within 5 m**. So this is a nearest-way lookup with a heading check, not
probabilistic map-matching from noisy GPS.

They are *not* quantised to a shared node network (367k distinct coordinates out
of 503k points in rider A's trips; the heavy repeats are parked locations, not
nodes), so we cannot recover BMW's own road IDs — but we do not need to.

Each point maps to `(segment_id, chainage)`.

### Kernel attribution instead of hard binning

A point is not assigned to exactly one unit. Its contribution is weighted, which
removes every arbitrary boundary from the pipeline.

**Perpendicular weight — resolves snapping ambiguity:**

```
w_snap(i, s) = exp( −d⊥(i,s)² / 2σ⊥² ) × max( 0, cos Δbearing(i,s) )
σ⊥ ≈ 20 m       # matched to the observed 16 m offset
```

A point between a motorway and its frontage road contributes *partially to both*
rather than being confidently wrong. The heading term does most of the
separating work, since parallel roads are usually travelled at similar bearings
but junction approaches are not.

**Along-road weight — fixes the split-corner problem:**

```
w_along(i, x) = exp( −(chainage(i) − x)² / 2σ∥² )
σ∥ ≈ 50–100 m
```

A corner's lean-change belongs partly to its approach and partly to its exit, so
it is spread along the road rather than hard-cut at a chainage boundary. This is
precisely the failure that made a grid unacceptable.

**Two rules that bite if ignored:**

1. **Normalise by the weight sum, not the point count** (Nadaraya–Watson):

   ```
   score(s) = Σᵢ wᵢ·xᵢ / Σᵢ wᵢ
   ```

   Otherwise segments at the edge of coverage score artificially low, and the
   router systematically avoids roads merely for being at the boundary of the
   data.

2. **Never smooth across a junction.** A kernel that leaks lean data from one
   road, around a corner, into a different road is simply wrong. Smooth along
   chainage *within a road*; stop at junctions.

`Σw` is itself useful: it is the **snapping confidence**, and feeds the
confidence blend in §5.

### What we compute per segment

| Feature | How | Column |
|---|---|---|
| `lean_delta` | Σ \|Δlean\| per km, ignoring steps < 2° as noise | `sensorsbankingangle` |
| `leaned_share` | share of distance ridden with \|lean\| > 5° | `sensorsbankingangle` |
| `lean_p50`, `lean_p95` | percentiles of \|lean\| | `sensorsbankingangle` |
| `speed_mean`, `speed_p85` | observed, **not** the speed limit | `ridingvehiclespeed` |
| `band_share` | share of samples in 50–120 km/h | `ridingvehiclespeed` |
| `crawl_share` | share of samples under 20 km/h | `ridingvehiclespeed` |
| `abs_rate` | ABS activations per km | `ridingabsbraking` |
| `hard_decel_rate` | samples under −3 m/s² per km | `sensorsaccelerationlongitudinal` |
| `elev_*` | elevation profile | `positionrawelevation` + DEM |
| `rpm_mean` | engine speed | `ridingenginespeed` |
| `n_trips`, `Σw` | distinct rides; snapping confidence | **the filename** |
| `speed_by_band` | median speed × weekday/weekend × 3 time bands | `ridingvehiclespeed` |

### Curviness — the key measure, and why it is lean-based

```
curviness = lean_delta / km        # degrees of lean change per kilometre
```

BMW's own definition (`app.js:188`), and cleverer than it looks. A long
constant-radius sweeper holds a near-constant lean angle and accumulates almost
no *change*. A series of corners swings left–right–left and racks up change
fast. So this measures **the thing riders actually enjoy** — direction changes —
rather than mere road shape.

> **This is the moat.** `sensorsbankingangle` is 100% filled in the crowd data,
> and phones cannot measure it. Competing planners infer curvature from map
> geometry; we read it off the motorcycle. Geometry misses camber, surface,
> sightlines and rhythm — a blind off-camber 50 m bend looks identical to a
> well-cambered one in OSM, and completely different in the data.

### Implementation note that will bite you

Δlean needs a window function:

```sql
lag(sensorsbankingangle) OVER (PARTITION BY filename ORDER BY timestampinmillis)
```

**Partition by `filename`, never by `trip_id`.** `trip_id` is only 56% filled —
partitioning on it drops ~44% of rows into one null partition and silently
corrupts every Δlean across trip boundaries. Use
`read_csv('.../**/*.csv', filename=true)`.

Kernel attribution means aggregation is a **weighted join over a ±3σ chainage
window**, not a plain `GROUP BY`. Still expressible in DuckDB, more expensive,
and entirely offline — it does not touch serving latency.

---

## 4. Stage ② — The graph: junctions as nodes, turns as arcs

Junctions are where nearly everything bad for a rider concentrates:
**standstills** (a red flag in the brief), **flow** and **clear road view** (two
green flags), elevated **intersection risk**, and **attention** — every junction
is a navigation decision, and on a bike checking your phone is both irritating
and slightly dangerous.

So junctions carry a real cost. Getting that cost right needs three specific
fixes, because the naive version fails badly.

### Fix 1 — a junction only costs you if you stop or turn

**The trap:** "fewest junctions" is secretly a **motorway-seeking** objective. A
motorway has almost no at-grade junctions — grade-separated interchanges, no
signals, long uninterrupted runs. A naive count would drive every route onto the
A95 and fight the scenic score rather than reinforcing it.

**The fix:** charge for what actually costs the rider, not for topology.

| Situation | Cost |
|---|---|
| **Turn** — you change from one road to another | attention + momentum |
| **Control against you** — signals, stop, give-way | measured delay |
| Flowing straight through on priority | **free** |
| Passing a side road where you have priority | **free** |
| Grade-separated interchange you do not leave | **free** |

Now a flowing B-road with twenty side roads you sail past costs about the same
in junction terms as a motorway — and the scenic and fun scores then correctly
separate them, which is their job. The junction term stops competing with the
scenic term.

### Fix 2 — do not count junctions, measure what they cost

**The trap:** counting forces you to invent a penalty unit and tune it against
travel time by feel. Too small and it does nothing; too large and the router
takes a 40 km detour to dodge three roundabouts.

**The fix:** measure the delay from the crowd trips, in seconds.

For each junction and each turn through it, take the crowd trips that made that
manoeuvre and compare the time actually taken against the time it would have
taken at approach speed:

```
delay(j, turn, band) = t_observed_through_junction
                     − ( distance_through / v_approach )

junction_cost = delay(j, turn, band) + attention_cost(turn)
attention_cost = small fixed ≈ 5 s, only when the named road changes
```

Three things this buys:

1. **It is in seconds** — automatically commensurate with travel time, so it
   *cannot* blow up into absurd detours. Ten junctions at ~15 s each is 2.5
   minutes; that can never justify a 40 km detour. **The unit is the bound.** No
   tuning constant to guess.
2. **It is measured, not tagged.** A signal that is green 90% of the time costs
   little, and the data knows that while OSM does not.
3. **It varies by time of day for free**, since crowd speed is already bucketed
   by time band.

**Fallback chain**, because thin data is common at turn granularity:

```
turn-specific measured delay
  → node-aggregate measured delay
    → tag-based prior (signals ≈ 15 s · stop ≈ 8 s · give-way ≈ 4 s · priority straight ≈ 0 s)
      → 0
```

**Verify this is real before relying on it.** Take ~20 known junctions and check
for a repeatable speed dip. At ~1 Hz sampling and 16 m matching error the dip may
be smeared out; if it is, junction cost falls back to tag-based priors and the
"measured, not tagged" claim comes off the slide.

### Fix 3 — turn costs need an edge-based graph

**The trap:** a *node* penalty is easy — push it onto incoming edges, everything
stays positive, Dijkstra unchanged. But a *turn* penalty depends on the **pair**
of edges: straight through is free, a left turn across traffic is not. A
node-based Dijkstra cannot express that, because by the time you are at the node
you have forgotten how you arrived.

**The fix: a turn-expanded graph.** Nodes become *directed segments*; arcs
become *permitted turns*.

```
node  := (segment, direction)
arc   := (incoming directed segment → outgoing directed segment) at a junction
cost(arc) = segment_cost(outgoing) + junction_cost(turn)
```

Every real router does this. Size grows to roughly `Σ(in-degree × out-degree)`
over junctions — call it 2–4× the node count, which is nothing at Bavaria scale.

Three things come free, and the third matters more than it sounds:

- **one-ways**, correctly
- **OSM turn restrictions** (`no_left_turn` relations)
- **U-turn prevention** — which is what stops the joyride loop from doubling
  back on itself at a dead end

Do this from the start. Retrofitting turn costs onto a node-based graph is
genuinely unpleasant.

### The dropout filter still applies

If a rider's GPS drops out, the data shows one "transition" spanning kilometres.
Snapping to real segments removes most of the damage, but still: **discard
point-to-point steps beyond ~150 m** when accumulating along-chainage features,
and require **≥ 2 distinct trips** before trusting a segment's crowd score. BMW's
own viewer guards the same way at `app.js:176`.

---

## 5. Stage ③ — Static enrichment, and scoring roads with no crowd data

Offline, segments are joined against the static external sources into
`segments_enriched.parquet`. Full detail in
[DATA_SOURCES.md](DATA_SOURCES.md); the features that matter here:

| From | Features |
|---|---|
| **OSM** | `curvature_geo`, `road_class`, `surface`, `maxspeed`, `tunnel`, `bridge`, `oneway`, turn restrictions, `viewpoint`, `water_prox`, `forest_share`, `construction` |
| **Copernicus DEM** | `gradient`, `elev_gain_per_km`, `relief`, `ridge_score`, `horizon_west` |
| **CLMS land cover** | `forest_frac`, `natural_frac`, `urban_frac`, `water_frac` |
| **Accident data** | `accident_rate` per million rider-km, split wet / dry / dark |

Tags now attach to the segment **natively** — no spatial join from a square to a
road, which was always an approximation.

### Geometric curvature, and the resampling trap

`curvature_geo` is computed from way geometry:

```
bearing(p₁,p₂) = atan2( sinΔλ·cosφ₂ , cosφ₁·sinφ₂ − sinφ₁·cosφ₂·cosΔλ )
curvature_geo  = Σ|Δbearing| / km
```

**There is no curvature API** — nobody serves it as a field. You fetch geometry
(Overpass `out geom;`, or a Geofabrik extract for bulk) and compute it yourself.

**The trap: OSM node spacing is wildly irregular.** A straight road may have
nodes 500 m apart; a hairpin may have forty nodes in 100 m — because a human
traced it from aerial imagery. Differentiating per *node* means a curve
accumulates more apparent heading change simply for being drawn in more detail,
and your curvature score partly measures OSM mapping density.

**So resample the polyline to uniform 10–20 m spacing before differentiating**,
then smooth lightly. This is testable: artificially densify a way's nodes and
assert curvature is unchanged.

### The confidence blend

`curvature_geo` measures the same physical property as lean-derived `curviness`,
**without needing any rider to have been there**. So we degrade gracefully
instead of going blind:

```
c(s)       = Σw(s) / (Σw(s) + k)                 # k ≈ 5 trips-equivalent
quality(s) = c · crowd_score + (1 − c) · geometry_score
```

A well-ridden road uses **measured lean**. An unridden road falls back to
**geometry and terrain**. The UI surfaces `c` so a judge can see which is which.

This is also the principled fix to BMW's own experimental `funFactor`
(`app.js:203`), which multiplies by `pathKm` — so a unit scores higher simply for
*containing more road*, regardless of quality. That is an exposure count hiding
inside a quality score. We normalise per kilometre and use trip count **only** as
confidence, never as a fun multiplier. Say this out loud on stage: it is the
strongest available signal that we actually read their data.

### Accident rate must be exposure-normalised

Raw counts would penalise exactly the roads riders love, because famous
motorcycling roads carry far more motorcycle traffic. The crowd data gives us the
denominator for free:

```
exposure(s)      = n_trips(s) × length(s)          # rider-metres
accident_rate(s) = motorcycle_accidents(s) / exposure(s)
```

A road is flagged only if it is dangerous **relative to how much it is ridden**.
That is the difference between a statistic and an insight. Aggregate to the named
road before trusting a rate — accident data is too sparse at 200 m.

---

## 6. Stage ④ — The rider profile, and the three ways it changes the route

The profile influences the route through **three separate channels**, and
keeping them separate is what makes the system explainable *and* safe.

| Channel | Question | Mechanism | Overridable? |
|---|---|---|---|
| **Taste** | what does this rider enjoy? | KPI **weights** | Yes — profile chips and the `α` slider |
| **Capability** | what can they handle safely? | **hard exclusions** and ceilings | **No. Never** |
| **Context** | when and how do they ride? | defaults, suggestions, notifications | Yes |

### Channel 1 — Taste: revealed-preference weights

Take the segments the rider has actually ridden, distance-weight them, and
compare each feature's mean against the crowd baseline:

```
z_R(f) = ( μ_R(f) − μ_crowd(f) ) / σ_crowd(f)      # per feature f
w_R(f) = normalise( clip(z_R(f), 0, ∞) )           # weights, Σw = 1
```

*Whatever this rider consistently over-indexes on becomes a heavier weight in
their personal scoring.* If their rides are 1.8× curvier and 300 m higher than
the average BMW rider, curviness and altitude dominate their weights.

About thirty lines of code, no training, no black box — and the weight vector
goes **straight onto a bar chart in the UI**. That is the whole reason to prefer
it to a fitted model: the brief promises bonus points for explainability, and a
judge can audit this on screen in five seconds.

The **Sportive / Safer / Chill** chips are priors on these weights. Personal data
moves them away from the chosen prior; with no personal data the prior is all you
get. So a brand-new rider still gets a sensible route, and the system gets more
personal with every ride — which is itself a slide.

### Channel 2 — Capability: the style ratio, and why raw lean is not enough

Naively, capability is the rider's p95 lean angle. That is **wrong in a way that
matters**: lean conflates the road with the rider. A fast rider on a motorway
logs less lean than a cautious rider on a mountain pass. Comparing raw lean
across riders compares the roads they happen to live near.

Normalise by the road. For every segment the rider shares with the crowd:

```
style_ratio(R) = median over shared segments (  rider_lean_p95(s)
                                              / crowd_lean_p95(s)  )
```

Now the number means something: **0.6 = rides well within the road's demands;
1.2 = rides harder than the crowd on the same tarmac.** Road-normalised, and
comparable between riders who never ride the same roads. Segments make this
sharper than cells did, because a segment is road-coherent.

The ceiling used for exclusions:

```
lean_ceiling(R) = crowd_lean_p95 × style_ratio(R) × safety_margin
EXCLUDE segment if crowd_lean_p95(s) > lean_ceiling(R)
```

**Capability is a hard exclusion, not a penalty.** A soft penalty can always be
overwhelmed by a large enough fun bonus — precisely the failure mode you cannot
ship in a motorcycle product. An exclusion cannot be outvoted.

Also derived: speed envelope relative to `maxspeed`, and braking behaviour from
`ridingabsbraking` + `sensorsaccelerationlongitudinal` (**not** brake pressure —
that column is 0% filled everywhere).

### Channel 3 — Context

- **Free-time window** — histogram of trip start times, weekday × hour, plus
  typical duration. *"You usually ride Saturday 09:00–13:00 — next window is dry
  and 19 °C."*
- **Weather history** — from the Open-Meteo **archive**, every past trip
  retroactively labelled with the conditions it happened in.
- **Bike class** — inferred from rpm-per-km/h, gear count, lean envelope, tyre
  pressure, and **confirmable in the UI**, because bike model is not in the data.
- **Novelty appetite** — how often they repeat roads versus ride new ones. Sets
  the default balance between *revisit a favourite* and *explore something new*.
- **Turn tolerance** — riders who consistently choose long uninterrupted roads
  get a higher junction weight. Measurable from their own history.

### Three things we must not claim

The dataset contains no rider **age**, no **bike model**, and no **road
construction**. All three appear in our whiteboard notes. "Usage of BMW Personal
Rider Data" is graded, and claiming a field we do not have is the one thing that
reads as fabrication to a BMW engineer. Profiles are behavioural only.

Also: `sensorsaccelerationlateral` is 70% filled on crowd data but **3% and 5%
for two of the three example riders**. It must never enter the personal profile —
it would pass every test against rider C and silently return nothing for two real
riders.

---

## 7. Stage ⑤ — The learning system

The whiteboard note is *"push riders to a safe limit so he learns while being
safe."* This section is that sentence turned into arithmetic. It is the
differentiator, so it is worth getting right.

### 7.1 The skill vector

| Dimension | Measure | Source |
|---|---|---|
| **Lean** | p90 of \|lean\|, plus `style_ratio` | `sensorsbankingangle` |
| **Curviness** | p90 of the curviness of segments ridden | crowd ∩ their trips |
| **Gradient** | p90 of gradient ridden, and max altitude | Copernicus DEM |
| **Conditions** | set of weather bands ridden in | Open-Meteo archive |
| **Terrain** | **road-kilometres ridden** of the region's rideable network | segment coverage |

Each dimension carries three numbers:

```
current_d   = rider p90 on dimension d          # what they have demonstrably done
ceiling_d   = safe maximum for this rider       # from capability, never crossed
Δ_d         = stretch increment                 # small, e.g. 10% of current
```

`current` uses **p90 rather than max** deliberately. A single lurid lean angle
from a near-miss or kerb strike is not a demonstrated capability, and building
progression off a maximum would ratchet the rider upward off one bad data point.

Note the Terrain dimension improves with segments: **"you've ridden 340 of
Bavaria's 4,200 km of good motorcycling road"** is a far better statement than a
percentage of grid squares.

### 7.2 What makes a segment a growth opportunity

```
stretch_d(s, R) = 1   if  current_d(R) < level_d(s) ≤ current_d(R) + Δ_d
                  0   otherwise
```

Then two gates, and the second is the important one:

```
GATE 1 (safety):   level_d(s) ≤ ceiling_d(R)  for every dimension d
GATE 2 (novelty):  |{ d : level_d(s) > current_d(R) }| ≤ 1
```

**Gate 2 — one novelty at a time — is the core safety idea of the whole
feature.** A road slightly curvier than usual, in familiar weather, on a familiar
road type, is a good place to learn. A road that is curvier *and* steeper *and*
wet is not — even though each factor individually sits inside the stretch band.
Compound novelty is how riders get hurt, and it is exactly what a naive
"maximise growth" objective would select for.

It also matches how skills coaching works in any physical discipline, which makes
it easy to defend in the room.

### 7.3 The challenge dose — a route-level budget

```
dose(route) = Σ length(s) over segments where stretch(s) = 1
              ──────────────────────────────────────────────
                          total route length

target dose:   Chill 5%   ·   Balanced 10–15%   ·   Sportive 20–25%
```

This is a **route-level constraint, not an edge cost** — Dijkstra cannot express
it, exactly like the time budget. Same solution: generate candidates across a
range of growth weights, measure the actual dose of each, return the one landing
in the target band. Six or seven candidates is plenty.

Keeping the dose an explicit, displayed number is also good product design: the
rider sees *"18% of this ride is new ground for you"* rather than trusting an
opaque challenge level.

### 7.4 Weather shrinks the ceiling — one mechanism, two problems solved

```
ceiling_d(R, t) = ceiling_d(R) × weather_factor(t)
weather_factor  = 1.0 dry  ·  ~0.7 wet  ·  ~0.5 cold + wet  ·  0 snow/ice
```

Because ceilings gate the stretch band, a wet forecast automatically collapses
growth to zero and the router falls back to comfortable roads. **No separate rule
is needed for "don't teach someone a new lean angle in the rain"** — it falls out
of the same arithmetic.

The accident data adds a second, independent veto: **never set a growth target on
a road with an elevated motorcycle-accident rate**, however well it matches the
stretch band. Learning happens on safe roads.

### 7.5 The feedback loop — how learning actually closes

1. **Propose** — the route carries a measured dose of stretch segments.
2. **Observe** — the rider rides it. New telemetry arrives.
3. **Compare** — on the stretch segments specifically:

```
realised(s) = rider_lean_p95_this_ride(s) / crowd_lean_p95(s)

realised ≥ style_ratio            → they met it.      current_d advances.
realised < style_ratio × 0.85     → they backed off.  current_d holds,
                                    and the next dose is reduced.
```

4. **Update** — recompute the skill vector, coverage and records.

The whole loop runs on telemetry alone. **We never have to ask the rider how it
went** — the bike already told us. *"You took the Kesselberg at 0.8× your usual
lean. We won't push that one again yet."* That sentence, from data the rider
never entered, is the demo moment that sells the thesis.

### 7.6 What the rider sees

| Surface | Content |
|---|---|
| **Learnings** | Progression per dimension; "your next challenge" as a **named road**, with the gates shown |
| **Records** | New max lean, max altitude, longest ride, first wet ride, most curvature in one ride |
| **Explore (fog map)** | Road-kilometres ridden vs the region's network; unexplored **fun-dense** roads glowing as targets |
| **Route badges** | `Stretch your lean` · `New terrain` · `New conditions` |

---

## 8. Stage ⑥/⑦ — Dynamic context and scoring

Dynamic per request: **weather** (Open-Meteo forecast, sampled along the route at
the hour the rider will actually be there — not one value for the whole ride) and
**live traffic** where configured, else the crowd prior:

```
congestion(s, t) = 1 − ( speed_median(s, band(t)) / speed_p85(s) )
```

Four composite scores. Every input is normalised **per kilometre** and
percentile-scaled against the crowd, so all terms are comparable and
dimensionless.

| Score | Inputs |
|---|---|
| **Scenic** `f(t)` | curviness · `forest_frac` · `water_prox` · `viewpoint` · `relief` · `ridge_score` · `elev_gain_per_km` · −`urban_frac` · −`tunnel` · −motorway penalty · **sunset window** · −congestion |
| **Fun** | curviness (crowd) blended with `curvature_geo` by confidence · `leaned_share` · `band_share` (the 50–120 km/h green flag) · `gradient` · **flow** · `rpm_mean` |
| **Risk** | `accident_rate` (condition-matched) · `abs_rate` · `hard_decel_rate` · `surface` · `precip` · `temp < 8 °C` · `wind_gust` · `visibility` · congestion · capability gap |
| **Growth** | `stretch` on any dimension, subject to both gates (§7.2) |

### Flow, now that it is expressible

On a grid, flow was reduced to a junction count per km. On segments it becomes
what riders actually mean:

```
flow(s) = (1 − crawl_share)
        × uninterrupted_km_until_next_stop_or_turn
        × corner_rhythm                              # optional layer
```

`corner_rhythm` needs the corner-event layer: detect each corner as a contiguous
run of \|lean\| above threshold, then score the *sequence* — spacing regularity,
left-right alternation, radius consistency. *"Nine corners in 4 km, alternating,
radii within 20% of each other."* That is flow, and it is a sequence property a
grid cannot represent.

Corner events also sharpen capability matching: not "this road needs 45°" but
"corner 7 of 14 needs 45°, the rest are fine". Because steady-state lean
satisfies `tan θ = v²/(g·r)`, corner radius is estimable from telemetry alone —
treat it as a band, not a number, since riders are not in steady state and camber
shifts it.

### The sunset window

Sun azimuth and elevation are computed locally (NOAA formula, ~20 lines, no API).
But azimuth alone says *where the sun is*, not whether the rider can see it.
Sampling the DEM along that bearing gives `horizon_west`: is the view toward the
sun open, or is there a mountain in the way. That makes the joyride showpiece
defensible rather than decorative.

---

## 9. Stage ⑧ — The cost function

Per arc of the turn-expanded graph — an outgoing directed segment reached through
one specific turn:

```
value(s,t) = w_scenic·scenic(s,t) + w_fun·fun(s) + w_growth·growth(s,R)
risk(s,t)  = w_acc·accident_rate + w_abs·abs_rate + w_dec·hard_decel
             + w_wx·weather(t) + w_tr·congestion(s,t) + w_surf·surface
time(s,t)  = length(s) / v(s, band(t))

cost(arc) = time(s,t) · ( 1 + α·(1 − value(s,t)) + β·risk(s,t) )
          + junction_cost(turn, band(t))              ← measured, in seconds

EXCLUDE arc if:  crowd_lean_p95(s) > lean_ceiling(R, t)   # capability, weather-adjusted
                 accident_rate(s) > severity_threshold     # accident outliers
                 construction or closure on s              # OSM / live traffic
                 surface forbidden by rider options        # e.g. dirtRoads=forbid
                 snow or ice at t
                 turn not permitted                        # one-way, restriction, U-turn
```

Five properties that matter:

1. **Every term is positive.** `time > 0`, the multiplier is ≥ 1, and
   `junction_cost ≥ 0`. No negative-weight arcs, so Dijkstra is provably correct.
2. **`α` is the single user-facing dial** — the *Chill ↔ Sportive* slider.
3. **Junction cost is additive and in seconds**, not multiplied by `α`. It is a
   real delay, not a matter of taste, so `α` must not be able to wish it away.
   And because the unit is seconds, it is self-bounding.
4. **Safety is exclusion, not penalty.** Unoutvotable by design.
5. **Speed has a documented fallback chain:** segment median → named-road band
   median → regional median. Never let a missing `v` produce a divide-by-zero
   shortcut.

### A worked example

Munich → Kesselberg. One 200 m segment on each candidate road, β = 1:

| | A95 motorway | Kesselberg pass |
|---|---|---|
| Observed speed | 120 km/h | 50 km/h |
| Real time | 6.0 s | 14.4 s |
| `value` | 0.10 | 0.90 |
| `risk` | 0.05 | 0.20 |

**At `α = 0`** (pure speed):
`motorway = 6.0 × 1.05 = 6.3` vs `pass = 14.4 × 1.20 = 17.3` → **motorway wins.**

**At `α = 3`** (sportive):
`motorway = 6.0 × (1 + 3(0.90) + 0.05) = 6.0 × 3.75 = 22.5`
`pass     = 14.4 × (1 + 3(0.10) + 0.20) = 14.4 × 1.50 = 21.6` → **the pass wins.**

Same algorithm, same data, one slider. **This inversion is the primary
correctness test** — if it does not flip, the cost function is wrong.

---

## 10. Stage ⑨ — Search

### Mode 1: Destination (A → B)

Dijkstra with a binary heap over the **turn-expanded** graph. Snap origin and
destination to the nearest segment.

```
dijkstra(turn_graph, start_dir_seg, goal_seg, cost_fn, t):
    dist[start] = 0;  heap = [(0, start)]
    while heap:
        d, u = pop_min(heap)
        if segment_of(u) == goal_seg: return reconstruct(u)
        if d > dist[u]: continue                  # stale entry
        for (v, turn) in permitted_turns(u):
            if excluded(v, turn, rider, t): continue
            nd = d + cost_fn(v, turn, t)
            if nd < dist[v]: dist[v] = nd; prev[v] = u; push(heap, (nd, v))
```

Turn expansion multiplies node count by 2–4×, which is still well under a second
in pure Python at Bavaria scale. Fewer, more meaningful edges than a grid.

**Three route options** — run at three values of `α` (Chill / Balanced / Full
Send). Genuinely different routes, because `α` reweights every arc.

**Time budget** — binary search on `α`: higher `α` means a longer, better route,
so search `α ∈ [0, α_max]` until duration lands within ±10% of the budget.

**Challenge dose** — the same candidate-and-measure loop over the growth weight
(§7.3). Both budgets are route-level constraints resolved by generating
candidates and measuring, never by distorting an arc cost.

Duration versus `α` **steps** rather than curving, because paths are discrete.
Treat monotonicity as a smoke test only.

### Mode 2: Joyride (X hours from here, return home)

The same Dijkstra, called four times. No second engine.

1. **Flood outward** from the origin once → travel time to every reachable
   directed segment.
2. **Take the ring** at ≈ T/2 travel time.
3. **Pick top-K turnarounds, spread across bearings**, so the three offered loops
   look and feel genuinely different rather than three variations on the same
   valley. Bias toward unridden roads to serve the Terrain dimension.
4. Route **out** on the value-weighted cost, then **back** with a reuse penalty on
   already-traversed segments — precisely BMW's own
   `alreadyUsedRoads="allow|forbid"` option, so we speak their vocabulary.
5. **Rank loops** by value per hour; return the best three.

Junction minimisation is strongly aligned with this mode: it naturally produces
the *"continuous curved scenic road"* from the whiteboard notes. And U-turn
prevention from the turn expansion is what stops a loop doubling back at a dead
end.

### Sunset scheduling

Because `scenic` is `f(t)`, the planner can *place* a west-facing leg inside the
golden-hour window rather than merely reporting when sunset is:

> *"Leave at 18:10 and you'll be at the Kesselberg overlook facing west at
> sunset."*

---

## 11. Stage ⑩ — How the algorithm explains itself

The brief states *"explainability + live demo will score bonus points"*, and asks
for "an overview of all used data sources and how they are weighted". Explanation
is a first-class output, not a slide.

**1. Your profile, as a bar chart.** *"We think you like curves and altitude —
across your 101 rides you ride 1.8× curvier roads than the average BMW rider."*
Sourced, not asserted. Switching rider re-profiles the whole dashboard live.

**2. Per-route KPI bars.** Fun, scenic, safety, growth, plus km, minutes,
curviness in °/km, elevation gain, % of time in the 50–120 km/h band.

**3. The map coloured by *why*.** Each segment tinted by whichever KPI earned its
place — and because segments are real roads, this reads cleanly instead of
blockily.

**4. The road-not-taken panel.** The fast route beside the chosen one:

> *"14 minutes slower. 3.2× the lean changes. 340 m more climb. **3 junctions
> instead of 11 — about 2 minutes less stopped.**"*

The junction line is now a concrete, checkable claim in seconds, not a vague
"fewer turns". That is the sentence that wins the room, because it states a
trade-off honestly instead of asserting a score.

**5. The learning card.** *"18% of this ride is new ground for you: the
Kesselberg section asks for about 8° more lean than you've ridden, in dry weather
on a road type you know. Accident rate there is below the regional average for
the traffic it carries."*

**6. Ride preview.** Predicted lean and speed gauges along a road the rider has
not ridden yet.

**7. Honest gaps.** Where crowd data is thin, show the confidence `c` and say the
score is geometry-derived — rather than scoring zero and quietly routing around a
perfectly good road.

---

## 12. Complexity and scalability

| Stage | Cost | Notes |
|---|---|---|
| ① Snap + kernel aggregate | one pass over 13 GB + weighted join | DuckDB, spills to disk; morton prefix buckets the snap candidates |
| ① Output | ~30 MB Parquet | **13 GB → 30 MB.** This reduction *is* the scalability answer |
| ② Turn expansion | `Σ(in-deg × out-deg)` | 2–4× node growth; trivial at region scale |
| ② Junction costs | one pass over crowd trips through each node | offline, by time band |
| ③ Enrichment | one join per source | offline, cached; DEM/land-cover are raster lookups |
| ④ Profile + skill vector | O(rider points) | cached per rider, invalidated on new trips |
| ⑨ Dijkstra | O(E log V) | well under a second |

Segments are independent, so aggregation is embarrassingly parallel and shards by
morton prefix. Scaling from Bavaria to Germany is more machines on the offline
pass, with identical serving cost.

---

## 13. Known limitations — state these before a judge finds them

- **Coverage is Bavarian.** The crowd lake concentrates at 47.6–48.5 N,
  11.0–11.9 E. Own it: *"BMW's crowd data is Bavarian, so that's where we
  demo."* OSM topology plus the confidence blend still lets us route outside it,
  on geometry rather than measured lean.
- **Snapping can confuse parallel roads.** A 16 m mean offset is fine on an
  isolated road; motorway-plus-frontage is the hard case. The heading term and
  kernel partial-credit mitigate it. **Test on the A95 corridor specifically.**
- **Junction delay may not be measurable.** At ~1 Hz and 16 m matching error the
  speed dip may smear out. Falls back to tag-based priors, and then the
  "measured, not tagged" claim comes off the slide.
- **Curviness conflates road and rider.** Mitigated by crowd aggregation,
  confidence weighting, and `style_ratio` on the personal side — not eliminated.
- **Traffic is a historical prior** unless a key is configured. Today's accident
  is invisible. A deliberate trade for demo reliability.
- **Accident data is sparse** at segment resolution. Aggregate to the named road
  before trusting a rate; treat low-exposure segments as unknown, not safe.
- **Sunset assumes the DEM tells the whole story.** Terrain horizon, not tree
  lines or buildings.
- **The learning loop needs rides to close.** With three riders and a fixed
  dataset we can *demonstrate* the update arithmetic on their history, but not
  months of progression. Be explicit that it is shown retrospectively on real
  data, not simulated forward.
- **`style_ratio` needs shared segments.** A rider with no crowd overlap has no
  road-normalised ratio; fall back to the Safer prior and say so.

---

## 14. Verification checklist

| Gate | Test |
|---|---|
| **Curvature resampling** | Artificially densify a way's nodes; assert `curvature_geo` is **unchanged**. If it moves, you are measuring OSM mapping density |
| **Snapping — parallel roads** | On the A95 corridor, assert motorway and frontage-road points separate. The heading term should do most of the work |
| **Kernel normalisation** | A segment at the edge of coverage must not score systematically low — check `Σw·x/Σw`, never `Σx/n` |
| **No kernel leak across junctions** | Inject synthetic high-lean data on one road at a junction; assert the *other* road's score does not move |
| **Turn expansion** | One-ways respected; no U-turns in any output route; OSM `no_left_turn` relations honoured |
| **Junction delay is real** | ~20 known junctions show a repeatable speed dip; measured delays order sensibly (signals > give-way > priority straight) |
| **MOTORWAY TRAP REGRESSION** | Enabling junction cost must **not** increase the motorway share of generated routes. This is the guard on Fix 1 — if motorway share rises, junction cost is being charged for topology instead of for stopping and turning |
| **Junction cost is bounded** | Total junction cost on a route stays a small share of total time; no route detours more than a few km to avoid junctions |
| **Cost inversion** | Munich→Kesselberg takes the pass at high `α`, the A95 at `α = 0` |
| **Confidence blend** | A segment with no crowd data still receives a fun score, from geometry; `c` reported as ~0 |
| **Accident normalisation** | A popular fun road does **not** outrank a quiet dangerous one — if it does, the exposure denominator is wrong |
| Time budget | Binary search lands within ±10% of the requested duration |
| Challenge dose | Measured dose lands in the profile's target band; `dose = 0` on a wet forecast |
| **Gate 2 (novelty)** | Construct a segment that is a stretch on lean *and* gradient *and* conditions; assert it is **not** offered as growth |
| Ceiling shrink | Same request dry vs wet → wet excludes the stretch segments |
| Safety exclusion | A synthetic low-`style_ratio` rider has the Kesselberg segments **excluded**, not merely penalised |
| Feedback loop | Replay a rider's trips chronologically; `current_lean` is monotone non-decreasing and never advances on a ride where `realised < style_ratio × 0.85` |
| Profiles differ | Riders A, B, C give three different weight vectors *and* three different style ratios |
| Joyride | Returns to origin; legs substantially non-overlapping; three loops differ in bearing; no U-turns |
