# The Dashboard

A single-page app. **The rider switcher is pinned in the header** — A / B / C,
plus *Import rider* pointing at any folder of `recordedTrips`. Every view reacts
to it, so switching rider mid-demo re-profiles the whole dashboard live.

That is a strong on-stage moment: **same road, three riders, three different
scores and three different routes.**

---

## Seven views

### 1. Overview — the greeting

- *"Good evening, Rider A."*
- Weather now, plus the forecast across their likely ride window
- **Detected free time**, from their own history: *"you usually ride Saturday
  09:00–13:00 — next window is dry and 19 °C"*
- One hero suggestion card
- Latest records

### 2. Plan — the core

| Control | Effect |
|---|---|
| Mode toggle **Destination / Joyride** | which search strategy runs |
| Profile chips **Sportive / Safer / Chill** | seeds the KPI weights |
| Time budget | binary-searches `α` to hit it |
| **Start time** | drives every `f(t)` KPI — sun position, weather, traffic |
| **Challenge dose** | how much of the ride should be new ground: 5% / 10–15% / 20–25% |
| Bike confirm | corrects the inferred bike class |

A wet forecast collapses the challenge dose to zero automatically and the control
shows why — the rider's safety ceilings shrink in the rain, so there is nothing
in the stretch band to offer.

The map shows **three route options** with the crowd fun-heat layer underneath
and scenic stops pinned. Per route: KPI breakdown bars and a **"Why this route"**
card naming the weights and the data behind each.

**Ride Preview** plays the route with predicted lean-angle and speed gauges.

### 3. Suggestions — card feed

Mini-map thumbnail, title, scenic score, duration, and badges:
`New terrain` · `Sunset window` · `Revisit` · `Stretch your lean`.

Cards come from `/riders/{id}/suggestions`. Clicking one loads it into **Plan**.

### 4. Past trips

List plus map. Per trip: metrics, riding style, records set, and **emoji emotion
tagging** (persisted to a local JSON file). Any trip loads into the viewer with
its real telemetry.

### 5. Explore — the fog map

Fog-of-war over the road network itself — which roads the rider has actually ridden.
*"You've ridden 340 of Bavaria's 4,200 km of good motorcycling road."* Naming
road-kilometres rather than a percentage of grid squares is both more meaningful
and more motivating.

Unexplored but **fun-dense** roads glow as targets. This is where the
New-terrain growth KPI becomes visible, and it is the most screenshot-able view
in the app.

### 6. Learnings

The **skill vector** rendered — five dimensions, each with the rider's
demonstrated level, their safe ceiling, and the stretch band between them:

| Dimension | Shown as |
|---|---|
| Lean | current p90, ceiling, and the road-normalised style ratio |
| Curviness | °/km of the roads they ride |
| Gradient | steepest ridden, max altitude |
| Conditions | which weather bands they have actually ridden in |
| Terrain | road-km ridden of the region's network (links to the fog map) |

Progression over time per dimension, plus a records wall (max lean, max
altitude, longest ride, first wet ride, most curvature in one ride).

**"Your next challenge"** — a concrete named road whose demand sits just above
the rider's p90, **with the gates shown**: which single dimension is being
stretched, that every other dimension stays inside the ceiling, and that the
road's accident rate is below the regional average for the traffic it carries.

Showing the gates is the point. It is the difference between "here's a harder
road" and "here's a harder road, and here is why it is still a safe one".

### 7. Area metrics

The crowd layer per road segment, with a switchable metric: curviness / lean /
speed / ABS rate / traffic by time band / **accident rate per rider-km** /
**crowd-data confidence**.

The confidence layer is worth having on screen for the pitch — it shows exactly
where the scores come from measured lean versus road geometry, which is the
honest answer to "what happens where you have no data?"

This is the brief's *"visual representation of the data and algorithm"*
deliverable — and it doubles as our own debugging tool.

---

## Explainability is a UI feature, not a slide

The brief states *"explainability + live demo will score bonus points"*. Six
things carry it:

1. **Rider profile as a bar chart** — *"we think you like curves and altitude,
   because across your 101 rides you ride 1.8× curvier roads than the average
   BMW rider."* Sourced, not asserted.
2. **Per-route KPI bars** — fun, scenic, safety, growth, km, minutes, curviness
   in °/km, elevation gain, % of time in the 50–120 km/h band.
3. **The map coloured by *why*** — each stretch tinted by whichever KPI earned
   its place. This bit is here for the corners, this one for the lake, this one
   is just the connection out of town.
4. **The road-not-taken panel** — the fast route beside the chosen one, with the
   difference decomposed: *"14 minutes slower. 3.2× the lean changes. 340 m more
   climb. 3 junctions instead of 11 — about 2 minutes less stopped."* The
   junction line is a concrete, checkable claim in seconds rather than a vague
   "fewer turns". The sentence that wins the room, because it states a trade-off
   honestly instead of asserting a score.
5. **The learning card** — *"18% of this ride is new ground for you: the
   Kesselberg section asks for about 8° more lean than you've ridden, in dry
   weather on a road type you know. Accident rate there is below the regional
   average for the traffic it carries."* One dimension named, the gates shown,
   the safety evidence cited. The stretch segments are highlighted on the map.
6. **Ride preview** — predicted lean and speed gauges moving along a road the
   rider has not ridden yet.
7. **Honest gaps** — where crowd data is thin the UI shows the confidence value
   and says the score is geometry-derived, rather than scoring zero and quietly
   routing around a perfectly good road.

---

## Scope tiers

Decide with these, not by feel. **If it is not running on the laptop by the
feature freeze, it does not go on a slide.**

**Demo-critical**
Overview · Plan (both modes) · Ride Preview · Area metrics · rider switching ·
the learning card on a planned route · offline hardening

**Should-have**
Suggestions cards · Fog map · Learnings (full skill vector) · accident layer ·
DEM gradient

**Cut first, without apology**
Emoji tagging · records wall · notifications · live traffic · CLMS land cover ·
music

Music has **no data behind it** and should not reach a slide. Live traffic needs
an API key and the crowd prior already covers congestion — wire the interface,
demo without it.

One exception to the cut list: **the learning card is demo-critical even if the
full Learnings view is not.** The rider-development thesis is what the pitch
leads with, so at least one route must visibly say which single dimension it
stretches and why that is still safe.
