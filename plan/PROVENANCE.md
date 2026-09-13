# Where every number comes from

Anything marked **(\*)** in the code is a value **we chose ourselves**. It does
not come from the BMW dataset, from an API, or from the BMW brief. It is a tuned
guess, and it should be treated as one.

Everything *not* marked is traceable to a source, named below.

This file exists because "where does that number come from?" is the first
question a BMW engineer will ask, and the honest answer differs sharply
depending on which number you point at.

---

## Measured — from the BMW dataset

No judgement involved. These are read straight off 85,699 crowd trips and the
example riders' own telemetry.

| Quantity | Column |
|---|---|
| Lean angle, lean change, curviness | `sensorsbankingangle` |
| Observed speed, speed bands, crawl share | `ridingvehiclespeed` |
| ABS engagements | `ridingabsbraking` |
| Hard deceleration | `sensorsaccelerationlongitudinal` |
| Engine speed | `ridingenginespeed` |
| Ambient temperature (rider's weather history) | `sensorsoutsidetemperature` |
| Trip start points, durations, distances | `timestampinmillis` + map-matched position |
| Rider style ratio, lean p90/p95 | derived from the above |
| Which roads a rider actually chose | map-matched position snapped to segments |

Two thresholds here are **BMW's own, not ours** — taken from the `tripViewer`
they shipped so our curviness matches the definition they published:

- `LEAN_STEP_NOISE = 2.0` — `app.js:52`
- `LEAN_STRAIGHT = 5.0` — `app.js:56`

And one is empirical rather than assumed: `ABS_ENGAGED = 2`, established by
checking the actual value distribution (0/1/2/3, with 1 dominant).

## Measured — from external APIs

| Quantity | Source | Key needed |
|---|---|---|
| Road geometry, class, name, oneway, tunnel, surface, `maxspeed` | OpenStreetMap via Overpass | no |
| Lakes, peaks, forest, viewpoints | OpenStreetMap via Overpass | no |
| Elevation, gradient, local relief | Copernicus DEM GLO-30 | no (AWS mirror) |
| Rain, temperature, wind, forecast | Open-Meteo | no |
| Sun elevation and azimuth | computed locally, NOAA formula | no |

Sun **angles** are real astronomy. Civil twilight ending at −6° and nautical at
−12° are definitions, not opinions.

## From the BMW brief

- The 50–120 km/h band is a named green flag.
- Curves, elevation, scenic views, flow and clear road view are named green flags.
- Inner city, standstills, bad weather and bad road conditions are named red flags.

The brief says these things **matter**. It does not say how much. Every weight
is ours.

---

## (\*) Invented — our judgement, not anyone's data

### The scenic score itself
Every weight in `engine/scoring.py`: `W_CLASS 0.22`, `W_ELEV 0.15`,
`W_RELIEF 0.15`, `W_FOREST 0.10`, `W_WATER_VIEW 0.10`, `W_CURVE 0.28`,
`TUNNEL_PENALTY 0.35`. **The entire definition of "scenic" is our opinion.**
The inputs are measured; their relative importance is not.

### `CLASS_SCENIC` — the most opinionated table in the project
Motorway 0.02, tertiary 0.90, unclassified 0.85, and so on. The direction is
defensible from the brief; the numbers are invented.

### Risk weights
`W_ABS 0.45`, `W_DECEL 0.25`, `W_CRAWL 0.30`, `W_WEATHER 1.00`. The events are
counted from real telemetry. How much each one should scare you is ours.

### Junction costs — a placeholder for a measurement
`TURN_ATTENTION_S = 5.0`, `JUNCTION_DEGREE_S = 3.0`.

The design in `plan/ALGORITHM.md` §4 calls for reading junction delay **off the
crowd data** — observed speed drop per junction, per turn, per time band, in
seconds. That is not built yet. These two constants stand in for it. **Do not
claim junction cost is measured.**

### Traffic
`TRAFFIC_CLASS_PRIOR` is a guess per road class. The designed version derives
congestion from crowd median speed by time band (§3). Also not built yet.

### Rider capability and classification
- `CAPABILITY_MARGIN 1.15`, `BIKE_MARGIN`, `LEAN_MARGIN_DEG 8.0` — how much
  headroom is "safe" is a judgement no dataset states.
- `SPORT_RPM_PER_KMH 60`, `ROADSTER_RPM_PER_KMH 42` — **bike model is absent
  from the dataset entirely.** The class inference and its thresholds are ours.
- `EXPERIENCE_GATES` / `EXPERIENCE` — there is no skill label, licence class or
  experience field anywhere in the data. The bands and their names are invented.

### Weather response
The readings are real Open-Meteo values. Everything about how we react is ours:
rain → ×0.7 capability, cold rain → ×0.5, snow → ×0.0, and each risk increment.
Nothing measures how much grip a rider actually loses in the wet.

### Time-of-day scenic factors
Daylight 1.0 / 0.95 / 0.55 / 0.25 / 0.12 by light band, the golden-hour window
(−4° to +12°, a photographers' rule of thumb), and the 0.30 facing bonus.

### Scenic geometry thresholds
`SOUTH_SECTOR (140–235°)`, `VISIT_M 700`, `MIN_LAKE_M2 150,000`,
`MIN_PEAK_M 1,000`. Where "you can see it from the road" and "big enough to
count" begin are our lines.

### Pipeline and region choices
`CHUNK_M 100`, `SNAP_MAX_M 45`, `MAX_STEP_M 150`, `MIN_TRIPS 2`,
`CONFIDENCE_K 5`, `INDEX_LEVEL 16`, the demo bounding box, and which road
classes count as "rideable" (residential, service and track are excluded —
an opinion with consequences: a start point on a quiet street snaps to the
nearest larger road).

---

## What this means for the pitch

Say it plainly, because it is a strength rather than a weakness:

> **The inputs are measured. The scoring is our opinion.** Lean angle,
> curviness, speed, ABS events, terrain and weather are all real. What makes a
> road *scenic* is a definition we wrote, and every weight is in one file where
> you can argue with it.

Two things to be careful not to overclaim on stage:

1. **Junction cost is not measured yet** — it is a constant standing in for a
   crowd-data measurement we designed but have not built.
2. **Traffic is not measured yet** — same situation.

Both are marked (\*) in the code for exactly this reason.
