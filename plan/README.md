# BMW Ride Planner — Plan

Planning docs for the BMW Motorrad track ("FIND YOUR THRILL — Route Challenge")
at the TUM.ai Zurich hackathon. Pitch is the morning of **2026-09-13**.

Read in this order. Everyone needs `DATASET.md` regardless of which directory
they own.

| Doc | What's in it |
|---|---|
| [PROVENANCE.md](PROVENANCE.md) | **Where every number comes from.** Anything marked (*) in the code is a value we chose ourselves rather than measured — this is the inventory, and the answer to "where did that number come from?" |
| [DATASET.md](DATASET.md) | What the BMW dataset actually contains — measured column fill rates, dead columns, geography, and the three claims in our notes the data cannot support |
| [ALGORITHM.md](ALGORITHM.md) | **The main doc.** How a route is chosen end to end: segment snapping with kernel attribution, the junction-aware turn-expanded graph, the rider profile's three channels of influence, the learning system, KPI scoring, the cost function, both search modes, and how the algorithm explains itself |
| [DATA_SOURCES.md](DATA_SOURCES.md) | The six external sources — OSM, Copernicus DEM, CLMS land cover, Open-Meteo, live traffic, government accident data — what each contributes, what it costs, and the build priority |
| [STACK.md](STACK.md) | Tech stack, repo layout, API surface, the shared `Track` shape, demo hardening |
| [UI.md](UI.md) | The dashboard — seven views, rider switching, scope tiers |

---

## The thesis — what we lead the pitch with

From the whiteboard: *"Push riders to a safe limit so he learns while being
safe."*

Everyone else will build a router that finds twisty roads. Ours **develops the
rider** — steeper lean angles than they have ridden, terrain they have not
explored, weather they have not ridden in, each clipped by their own
demonstrated capability plus a safety margin.

That one idea unifies the whole dashboard: the fog map shows what is unexplored,
Learnings shows progression, records show milestones, and the Growth KPI is what
makes a road worth riding *today* rather than merely twisty. It is impossible
without lean-angle telemetry, which is BMW's moat — **phones don't measure lean
angle**.

> Strava tells you what you did. We tell you what you're ready for.

---

## The algorithm in six lines

1. Take OSM roads, **split them at junctions**, and subdivide to uniform ~200 m
   pieces. Junctions are nodes, segments are edges — the scoring unit and the
   routing unit are the same object, so routes follow real roads.
2. Snap 85,699 real rides onto those segments with **kernel attribution** —
   weighted by distance and heading rather than hard-binned, so a hairpin is
   never split into two half-corners.
3. Measure what each segment did to a motorcycle: how much the bike leaned, how
   much the lean *changed*, how fast people actually went, whether ABS fired.
4. Enrich with terrain, land cover, weather and **exposure-normalised accident
   rates** — see [DATA_SOURCES.md](DATA_SOURCES.md).
5. Score each segment for scenic / fun / risk / growth, weighted by what *this*
   rider's telemetry says they like, and ceilinged by what they can safely handle.
6. Run Dijkstra over a **turn-expanded** graph on
   `cost = time × (1 + α·(1 − good) + β·risk) + junction_delay`. One slider, `α`,
   turns a commute into a ride.

Full derivation, worked example and pseudocode in [ALGORITHM.md](ALGORITHM.md).

## Why segments and not a grid

A 100 m square is not a road. It can hold a motorway and its frontage road, or a
junction, or half a corner — and **a hairpin straddling a boundary is measured as
two half-corners**, degrading the single best feature we have because of an
arbitrary line on a map. "Flow" is a green flag in BMW's brief and it is a
property of a *sequence* of corners, which binning destroys outright.

`morton_code` survives as a **spatial index** — prefix queries, bucketing the
snap join. It is no longer what we score.

## Junctions cost seconds, not points

Junctions are where standstills, risk and navigation load all concentrate. Three
things had to be right:

1. **A junction only costs you if you stop or turn.** Naive "fewest junctions" is
   secretly a *motorway-seeking* objective — motorways have almost no at-grade
   junctions. Passing a side road with priority is free.
2. **Measure the delay, don't count the junctions.** Crowd speed profiles give an
   observed delay **in seconds** per turn per time band. The unit is the bound:
   ten junctions at ~15 s is 2.5 minutes, which can never justify a 40 km detour.
   No tuning constant to guess.
3. **Turn costs need a turn-expanded graph** — nodes are directed segments, arcs
   are permitted turns. That is also where one-ways, OSM turn restrictions and
   U-turn prevention come from free.

There is a regression test for trap 1 in the verification list: enabling junction
cost must **not** increase the motorway share of routes.

## How the rider profile actually changes the route

Three separate channels, and keeping them separate is what makes the system both
explainable and safe:

| Channel | Question | Mechanism | Overridable? |
|---|---|---|---|
| **Taste** | what do they enjoy? | KPI weights, fitted from their own rides | Yes |
| **Capability** | what can they safely handle? | **hard exclusions** | **No. Never** |
| **Context** | when and how do they ride? | defaults, suggestions | Yes |

Capability uses a **road-normalised style ratio** — the rider's lean compared to
the crowd's lean *on the same segments* — because raw lean angle conflates the
road with the rider. A cautious rider on a mountain pass out-leans a fast rider
on a motorway.

## How the learning works

The whiteboard line is *"push riders to a safe limit so he learns while being
safe."* In practice:

- A **skill vector** tracks five dimensions: lean, curviness, gradient,
  conditions, terrain.
- A road is a **stretch** if it asks for just above what the rider has
  demonstrably done — and still below their ceiling.
- **One novelty at a time.** A road that is curvier *and* steeper *and* wet is
  rejected even though each factor alone is in band. Compound novelty is how
  riders get hurt, and it is exactly what a naive "maximise growth" objective
  would select for.
- A **challenge dose** caps stretch at 5–25% of route distance by profile. A
  learning ride is mostly comfortable.
- **Wet weather shrinks the ceiling**, so growth opportunities collapse to zero
  on a rainy day with no separate rule needed.
- The loop **closes from telemetry alone**: if the rider took the stretch section
  at 0.8× their usual lean, they backed off, the level does not advance, and the
  next dose shrinks. We never have to ask how it went — the bike already said.

---

## The narrative slide

Of rider A's 89 planned routes, 74 carry route options. **69 of those 74 (93%)
are set to `optimization="fastest"`, and `windingness` sits on the `medium`
default in every single one.** Yet his lean traces show a rider hunting corners.

> We don't ask riders what they want — we read it off the motorcycle.

---

## How this maps to BMW's grading

The brief names its evaluation metrics explicitly. Each one is earned:

| Rubric metric | Delivered by |
|---|---|
| Usage of BMW **Crowd Data** | 85,699-trip segment aggregate (lean, curviness, observed speed, ABS) and the exposure denominator for accident rates. Junction delay and the congestion prior are *designed* to come from crowd data but are currently constants marked (*) — see [PROVENANCE.md](PROVENANCE.md) |
| Usage of BMW **Personal Rider Data** | Revealed-preference weights, road-normalised style ratio, skill vector, fog map, records, growth targeting |
| Usage of **External Sources** | OSM · Copernicus DEM · CLMS land cover · Open-Meteo (forecast **and** archive) · live traffic · government accident data — [DATA_SOURCES.md](DATA_SOURCES.md) |
| **Scalability & Efficiency** | DuckDB over Parquet, morton-prefix bucketing of the snap join: 13 GB → ~30 MB served |
| Calculation of **Fun Score** | The KPI scorecard, normalised per-km, explainable live on screen |
| Consideration of **Rider Safety** | Capability ceiling as a *hard constraint*, exposure-normalised accident rates, one-novelty-at-a-time gate, weather-shrunk ceilings |

Deliverables they ask for: the exact definition of "good route", an overview of
all data sources and their weighting, a visual route-planning tool, and a live
demo with an algorithm walk-through — *"explainability + live demo will score
bonus points."*

---

## Ground rules

- **The dataset stays out of this repo.** It lives at
  `~/Desktop/BMW/exd_download/` — 14 GB.
- **Commit straight to `dev`.** `main` only through a PR, and only Krish merges.
- **One owner per top-level directory** — see [STACK.md](STACK.md). Freeze the
  API contract in the first 30 minutes; that is what stops three people blocking
  each other.
- **If it is not running on the laptop by the feature freeze, it does not go on
  a slide.**
