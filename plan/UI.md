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
| **Start time** | drives every `f(t)` KPI — sun position and traffic |
| Bike confirm | corrects the inferred bike class |

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

Fog-of-war over the level-14 morton squares the rider has visited.
*"You've explored 18% of Bavaria."*

Unexplored but **fun-dense** areas glow as targets. This is where the
New-terrain growth KPI becomes visible, and it is the most screenshot-able view
in the app.

### 6. Learnings

Progression over time: max lean, terrains explored, weather conditions ridden,
altitude reached. A records wall.

**"Your next challenge"** — a concrete road whose crowd lean demand sits just
above the rider's p90, with the safety reasoning shown alongside it.

### 7. Area metrics

The crowd layer per morton square, with a switchable metric: curviness / lean /
speed / ABS rate / traffic by time band.

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
   climb. One fewer inner-city crossing."* The sentence that wins the room,
   because it states a trade-off honestly instead of asserting a score.
5. **Ride preview** — predicted lean and speed gauges moving along a road the
   rider has not ridden yet.
6. **Honest gaps** — where there is no crowd data the UI says so, rather than
   scoring zero and quietly routing around a perfectly good road.

---

## Scope tiers

Decide with these, not by feel. **If it is not running on the laptop by the
feature freeze, it does not go on a slide.**

**Demo-critical**
Overview · Plan (both modes) · Ride Preview · Area metrics · rider switching ·
offline hardening

**Should-have**
Suggestions cards · Fog map · Learnings

**Cut first, without apology**
Emoji tagging · records wall · notifications · music · road construction · OSM
`maxspeed` enrichment

Music and road construction have **no data behind them** — they should not reach
a slide either way.
