# BMW Ride Planner — Plan

Planning docs for the BMW Motorrad track ("FIND YOUR THRILL — Route Challenge")
at the TUM.ai Zurich hackathon. Pitch is the morning of **2026-09-13**.

Read in this order. Everyone needs `DATASET.md` regardless of which directory
they own.

| Doc | What's in it |
|---|---|
| [DATASET.md](DATASET.md) | What the BMW dataset actually contains — measured column fill rates, dead columns, geography, and the three claims in our notes the data cannot support |
| [ALGORITHM.md](ALGORITHM.md) | **The main doc.** How a route is chosen, end to end: cell aggregation, graph construction, KPI scoring, the cost function, both search modes, and how the algorithm explains itself |
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

## The algorithm in five lines

1. Cut Bavaria into ~100 m squares (BMW's dataset already indexes them by
   `morton_code`).
2. Ask 85,699 real rides what each square is like — how much the bike leaned,
   how much the lean *changed*, how fast people actually went, whether ABS fired.
3. Build the road network out of **observed** square-to-square transitions, so
   we route on roads riders actually ride at speeds they actually ride them.
4. Score each square for scenic / fun / risk / growth, weighted by what *this*
   rider's own telemetry says they like.
5. Run Dijkstra on `cost = time × (1 + α·(1 − good) + β·risk)`. One slider, `α`,
   turns a commute into a ride.

Full derivation, worked example and pseudocode in [ALGORITHM.md](ALGORITHM.md).

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
| Usage of BMW **Crowd Data** | 85,699-trip cell aggregate → *is* the routing graph, plus the area-metrics view |
| Usage of BMW **Personal Rider Data** | Per-rider revealed-preference weights, lean envelope, fog map, records, growth targeting |
| Usage of **External Sources** | Open-Meteo weather, OSM water/forest/viewpoints, local sun-position maths |
| **Scalability & Efficiency** | DuckDB over Parquet + morton-prefix pruning: 13 GB → ~30 MB served |
| Calculation of **Fun Score** | The KPI scorecard, normalised per-km, explainable live on screen |
| Consideration of **Rider Safety** | Capability ceiling as a *hard constraint*, plus crowd ABS rate and weather |

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
