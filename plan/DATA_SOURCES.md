# External Data Sources

BMW's brief says *"Creativity Welcome — Weather, rain radar, scenic viewpoints,
traffic, etc. → Enrich your algorithm with any external source."* "Usage of
External Sources" is a graded criterion.

This doc is the **overview of all used data sources and how they are weighted
and combined** that the brief asks for as a deliverable. How each feature enters
the route cost is in [ALGORITHM.md](ALGORITHM.md).

---

## Two tiers, and why it matters

| Tier | Sources | When fetched | Demo risk |
|---|---|---|---|
| **Static** | OSM, Copernicus DEM, CLMS land cover, accident data | Once, offline → joined into `cells_enriched.parquet` | **None.** Ships with the build |
| **Dynamic** | Open-Meteo, live traffic | Per request, cached to `fixtures/` | Managed — frozen fallback on any network error |

Everything static is baked in before the pitch. Only two sources touch the
network at demo time, and both degrade to a cached file rather than failing.
**The demo must run with networking disabled.**

---

## Priority — what gets built when

The pitch is tomorrow morning. Ordered by value per hour of work:

| # | Source | Tier | Verdict |
|---|---|---|---|
| 1 | **OSM (Geofabrik extract)** | **must — load-bearing** | Defines the segments and junctions everything is scored and routed on. Not optional any more: the crowd-transition fallback exists, but OSM is the design |
| 2 | **Open-Meteo** | must | No key, tiny payload, powers safety + the conditions dimension of learning |
| 3 | **Unfallatlas accidents** | high | One small download, and it is the strongest possible safety-criterion evidence |
| 4 | **Copernicus DEM** | high | Gradient and relief; also fills the elevation gap where BMW's map-matched elevation is only 46% filled |
| 5 | **CLMS land cover** | defer | OSM `landuse` covers most of it tonight; CLMS needs registration and a large raster |
| 6 | **HERE / TomTom traffic** | optional | Needs an API key — the one real liability. Crowd-derived prior is the always-on default |

---

## 1. OpenStreetMap

OSM is no longer an enrichment source — **it defines the unit of analysis.** Ways
split at junctions *are* the segments everything else is scored on, and junction
nodes *are* the graph nodes. See [ALGORITHM.md](ALGORITHM.md) §2 and §4.

**What we take:** `highway=*` road class · way geometry · junction nodes ·
`maxspeed` · `surface` · `oneway` · turn-restriction relations ·
`highway=traffic_signals` / `stop` / `give_way` · `tunnel` / `bridge` ·
`landuse=forest` · `natural=wood` · `natural=water` · `waterway` ·
`tourism=viewpoint` · `highway=construction`.

**Access:** two mechanisms, for two different jobs.
- **Geofabrik Bavaria extract** (`.osm.pbf`, ~600 MB) — the road network and
  junction topology, parsed once offline. Use `pyrosm` or `osmium`. **This is
  the one we need**, because Overpass will time out or rate-limit on a
  whole-region query and we want every road once, not repeatedly at runtime.
- **Overpass API** — POIs and area polygons for the demo bbox, fetched once into
  `fixtures/`. **Never called live during the demo.**

**License:** ODbL. Attribution required — put "© OpenStreetMap contributors" in
the UI footer. Do this; it costs one line and its absence is noticeable.

### There is no curvature API

Worth stating plainly because it is a natural thing to go looking for: **no API
returns road curvature as a field.** You fetch geometry and compute it yourself.

```
# Overpass, if you want geometry for a small bbox
[out:json][timeout:90];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified)$"]
   (47.4,10.8,48.6,12.0);
out geom;
```

Then differentiate bearing along the polyline — formula and the **node-density
trap** (resample to uniform 10–20 m spacing first, or you end up measuring how
finely a human traced the road) in [ALGORITHM.md](ALGORITHM.md) §5.

Commercial alternatives exist but are not overnight options: **HERE** sells ADAS
road-geometry attributes including curvature, as licensed map data with a
procurement conversation attached. Worth knowing that **Adam Franco's
open-source `curvature` project** computes exactly this from OSM extracts,
explicitly for motorcyclists — the obvious reference implementation to
sanity-check our numbers against, and evidence the approach is well-trodden.

Also note: **gradient is not in OSM either.** Elevation comes from the Copernicus
DEM, sampled along the same resampled polyline.

### Junction cost comes from the crowd, not from tags

OSM tells us *where* the junctions are and what controls them. It does **not**
tell us what they cost a rider. That number is measured from BMW's own trips —
observed delay in seconds per junction per turn per time band — with the OSM
tags as the fallback prior when crowd data is thin. Full mechanism and the
fallback chain in [ALGORITHM.md](ALGORITHM.md) §4.

### Why this is load-bearing

**Geometric curvature is independent of lean data.** It means we can score a road
**that no BMW rider has ever ridden** — the crowd data alone can only cover where
riders have been. That is what the confidence blend in
[ALGORITHM.md](ALGORITHM.md) §5 exists to exploit.

**Topology is the other half.** Real junction structure gives us one-ways, turn
restrictions and U-turn prevention, which the turn-expanded graph needs. A graph
built purely from observed crowd transitions is the **fallback** (collapse chains
of degree-2 nodes into polylines — no download needed), but OSM is the strong
version.

**Derived features:**

| Feature | From | Feeds |
|---|---|---|
| `curvature_geo` | resampled way geometry, heading deltas | fun, where crowd data is thin |
| **junction topology** | nodes of degree ≠ 2 | **the graph itself** — nodes and turn expansion |
| **turn controls** | `traffic_signals` / `stop` / `give_way` | junction-cost fallback prior |
| **turn restrictions** | `no_left_turn` relations | permitted-turn set |
| `oneway` | tag | permitted-turn set |
| `road_class` | `highway=*` | scenic penalty for motorway; "inner city" red flag |
| `surface` | `surface=asphalt/gravel/…` | risk; hard exclusion when the rider forbids `dirtRoads` |
| `maxspeed` | tag | the 50–120 km/h sweet-spot band |
| `tunnel` | `tunnel=yes` | scenic penalty — no view in a tunnel |
| `bridge`, `viewpoint`, `water_prox`, `forest_share` | tags and polygons | scenic; wind risk on bridges |
| `construction` | `highway=construction` | hard exclusion |

BMW's own GPX route options (`dirtRoads`, `tunnels`, `ferries`, `tollRoads`,
`motorways`, `borderCrossings` — see [DATASET.md](DATASET.md)) map almost
one-to-one onto OSM tags. Implement them with their names.

---

## 2. Copernicus DEM

**What it is:** GLO-30 — a global 30 m digital elevation model, free.

**Access:** the `copernicus-dem-30m` open bucket on AWS S3, or the Copernicus
Data Space. Tiles are named by lat/lon, so the demo bbox is a handful of files.
Sample with `rasterio`.

**License:** free and open, attribution required.

**Why it earns its place:** BMW's `positionmapmatchedelevation` is only **46%
filled** in the crowd lake. The DEM gives complete, consistent elevation for
every road — including roads with no crowd coverage at all.

**Derived features:**

| Feature | Definition | Feeds |
|---|---|---|
| `gradient` | elevation change per metre along the road | fun (the "Elevation" green flag), risk when steep and wet |
| `elev_gain_per_km` | cumulative climb | scenic, and the terrain dimension of learning |
| `relief` | local elevation range within a ~2 km radius | "does this feel mountainous?" — scenic context |
| `ridge_score` | road sits on a local maximum | open views, and pairs with sunset |
| `horizon_west` | terrain profile sampled along the sun's azimuth | **the sunset KPI** — is the view to the sun actually open, or is there a mountain in the way |

`horizon_west` is the feature that makes the sunset claim honest rather than
arithmetic. Sun azimuth alone tells you where the sun is; sampling the DEM along
that bearing tells you whether the rider can actually see it.

---

## 3. Copernicus Land Monitoring Service (CLMS)

**What we take:** CORINE Land Cover (100 m raster) — forest, natural areas,
agricultural land, urban fabric, water bodies. Optionally the high-resolution
Tree Cover Density and Small Woody Features layers.

**Access:** CLMS download portal. **Requires free registration**, and the
rasters are large — this is the friction that makes it the one to defer.

**What it adds over OSM:** *area context* rather than discrete features. OSM
tells you a forest polygon is nearby; CORINE tells you **what fraction of the
land within 200 m of the road is forest**. That is exactly the whiteboard note
*"trees, not more (outside cities)"*, and the urban-fabric class gives an
objective version of the "inner city" red flag instead of inferring it from
speed.

| Feature | Definition | Feeds |
|---|---|---|
| `forest_frac` | % forest in a 200 m road buffer | scenic |
| `natural_frac` | % natural / semi-natural | scenic |
| `urban_frac` | % urban fabric | scenic penalty — objective "inner city" flag |
| `water_frac` | % water bodies | scenic |
| `agri_frac` | % agricultural | neutral context |

**Tonight's substitute:** OSM `landuse=forest` + `natural=water` polygons,
rasterised coarsely, get ~80% of this. Write the feature interface so CLMS drops
in later without touching the scorer.

---

## 4. Open-Meteo

**Access:** free, **no API key**, generous rate limits. Two endpoints, and we
need both.

### Forecast — the ride we are planning

Hourly rain, snowfall, temperature, wind speed and gusts, and visibility, sampled
along the candidate route at the hour the rider will actually be there — not one
value for the whole ride.

| Feature | Feeds |
|---|---|
| `precip_mm` | risk; hard exclusion above a threshold |
| `temp_c` | risk — below 8 °C is a red flag in the brief |
| `wind_gust` | risk, especially on bridges and exposed ridges |
| `visibility` | risk; kills the scenic and sunset terms |
| `snow` | hard exclusion |

Weather also **shrinks the rider's capability ceiling** — see
[ALGORITHM.md](ALGORITHM.md) §7. One mechanism then handles "do not try to teach
someone a new lean angle in the rain."

### Archive — the rides they have already done

This is the non-obvious use and it matters for the learning system.

The **historical archive endpoint** (ERA5 reanalysis) lets us retroactively
label **every past trip** with the weather it actually happened in, by joining
trip timestamp and location. That gives us the *conditions* dimension of the
rider's skill vector: *has this rider ever ridden in real rain? at 4 °C? in
wind?*

Without it, the only proxy is `sensorsoutsidetemperature`, which knows
temperature and nothing else. With it, "new conditions" becomes a measurable
growth dimension rather than a guess.

**Caching:** fetch once, write to `fixtures/weather/`, and ship a frozen
fallback the app uses on any network error.

---

## 5. HERE Traffic / TomTom Traffic

**What we would take:** live congestion (flow speed vs free-flow), incidents,
closures, roadworks.

**Access:** both require an **API key**. Free tiers exist and are sufficient.

### Be honest about this one

A key provisioned the night before a demo is the single most likely thing to
fail on stage — expired trial, rate limit, blocked venue network. So it is
**built as an optional overlay behind an interface, never on the critical path:**

```
congestion(cell, t) = live_traffic(cell, t)   if a live layer is configured
                      crowd_prior(cell, t)    otherwise        ← always available
```

**The default is the crowd prior**, derived from BMW's own 85,699 trips: median
speed per square per time band divided by that square's free-flow p85. That is
real rider behaviour, needs no network, and earns another crowd-data point on
the rubric rather than an external-source dependency.

What the live layer genuinely adds that the prior cannot is **incidents,
closures and roadworks** — today's accident, today's resurfacing. Those become
**hard edge exclusions**, and they are the reason to wire the interface even if
we demo without a key.

Position it on stage exactly that way: *"congestion we already know from BMW's
own fleet; what we'd buy from HERE is today's closures."*

---

## 6. Government accident data — the safety differentiator

**Source:** the German **Unfallatlas** (Statistisches Bundesamt / DESTATIS),
open geocoded road-accident data published per year and per federal state,
covering Bavaria. Each record carries WGS84 coordinates, severity, light and
road-surface conditions, and flags for which vehicle types were involved —
including a motorcycle flag.

**Access:** direct CSV/shapefile download, no key, no registration. Small.
*Verify the exact column names on download rather than trusting any schema
written here.*

**License:** open data (DL-DE / CC-BY style) — attribute it.

### The crucial correction: normalise by exposure

The naive version counts accidents per square and calls the high ones dangerous.
**That is wrong, and it would actively harm riders.** Famous motorcycling roads
have more accidents because they carry vastly more motorcycle traffic. Scoring
raw counts would penalise exactly the roads riders love, for being popular.

We have the exposure denominator already, from the crowd data:

```
exposure(cell)      = n_trips(cell) × path_m(cell)        # rider-metres observed

accident_rate(cell) = motorcycle_accidents(cell) / exposure(cell)
                      → accidents per million rider-km
```

Now a road is flagged only if it is dangerous **relative to how much it is
ridden**. That is the difference between a statistic and an insight, and it is
the kind of reasoning a BMW safety engineer will immediately recognise.

### Condition-conditioned risk

Because each record carries road-surface and light conditions, we can split the
rate:

```
accident_rate_wet(cell)   accident_rate_dry(cell)   accident_rate_dark(cell)
```

So the risk term responds to *today's* conditions per square, rather than
applying one global wet-weather penalty. A road that is fine dry and
disproportionately dangerous wet gets penalised only when it is actually raining.

### Where it enters

- **Risk term** — `w_acc · accident_rate(cell, conditions)`
- **Hard exclusion** above a severity threshold, on the worst outliers
- **The learning ceiling** — never set a growth target on a road with an
  elevated motorcycle-accident rate, however well it matches the rider's stretch
  band. Learning happens on safe roads.

That last rule is the one to say out loud. It is the concrete answer to *"push
riders to a safe limit so he learns while being safe."*

---

## Summary: every source, one table

| Source | Key? | Tier | Primary contribution | Feeds |
|---|---|---|---|---|
| BMW crowd lake (85,699 trips) | — | static | lean, curviness, observed speed, ABS, congestion prior, **measured junction delay**, **exposure denominator** | fun, risk, traffic, junction cost, confidence |
| BMW personal trips | — | static | revealed preference, style ratio, skill vector | weights, ceilings, learning |
| **OpenStreetMap** | no | static | **segments + junction topology**, geometric curvature, surface, turn restrictions, POIs | the graph itself, fun, scenic, risk, exclusions |
| **Copernicus DEM** | no | static | gradient, relief, ridges, **western horizon** | fun, scenic, sunset |
| **CLMS land cover** | registration | static | forest / urban / water area fractions | scenic |
| **Open-Meteo forecast** | no | dynamic | rain, temp, wind, visibility | risk, ceiling shrink |
| **Open-Meteo archive** | no | static | retroactive weather labels on past trips | learning: conditions dimension |
| **HERE / TomTom** | **yes** | dynamic | incidents, closures, roadworks | hard exclusions |
| **Unfallatlas** | no | static | exposure-normalised motorcycle accident rate | risk, learning ceiling |

### Attribution

OSM (ODbL), Copernicus (DEM and CLMS), Open-Meteo, and the Unfallatlas all
require attribution. One footer line in the UI covers all of them, and its
presence signals that we read the licences.
