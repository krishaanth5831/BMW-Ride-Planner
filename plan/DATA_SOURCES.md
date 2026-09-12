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
| 1 | **Open-Meteo** | must | No key, tiny payload, powers safety + the conditions dimension of learning |
| 2 | **OSM (Overpass + extract)** | must | Road type, geometry curvature, surface, POIs. Unlocks scoring roads with zero crowd data |
| 3 | **Unfallatlas accidents** | high | One small download, and it is the strongest possible safety-criterion evidence |
| 4 | **Copernicus DEM** | high | Gradient and relief; also fills the elevation gap where BMW's map-matched elevation is only 46% filled |
| 5 | **CLMS land cover** | defer | OSM `landuse` covers most of it tonight; CLMS needs registration and a large raster |
| 6 | **HERE / TomTom traffic** | optional | Needs an API key — the one real liability. Crowd-derived prior is the always-on default |

---

## 1. OpenStreetMap

**What we take:** `highway=*` road class · way geometry · `maxspeed` ·
`surface` · junction nodes · `tunnel` / `bridge` · `landuse=forest` ·
`natural=wood` · `natural=water` · `waterway` · `tourism=viewpoint` ·
`highway=construction`.

**Access:** two different mechanisms, for two different jobs.
- **Geofabrik Bavaria extract** (`.osm.pbf`, ~600 MB) — the road network, parsed
  once offline. Use `pyrosm` or `osmium`.
- **Overpass API** — POIs and area polygons for the demo bbox, fetched once into
  `fixtures/`. **Never called live during the demo.**

**License:** ODbL. Attribution required — put "© OpenStreetMap contributors" in
the UI footer. Do this; it costs one line and its absence is noticeable.

### Why this is more than a nice-to-have

**Geometric curvature is independent of lean data.** From way geometry we can
compute heading change per metre directly:

```
curvature_geo(way) = Σ |Δbearing| / length_km
```

That matters enormously, because it means we can score a road **that no BMW
rider has ever ridden**. The crowd graph alone can only route where riders have
been — this is the way out of that limitation (see the confidence blend in
[ALGORITHM.md](ALGORITHM.md) §9).

**It also upgrades the road graph itself.** The fallback design builds the
network purely from observed crowd transitions. With an OSM extract we get real
topology — every road, correctly connected, with one-ways and turn restrictions.
The strong version is a **hybrid**: OSM supplies topology and completeness,
crowd data supplies the quality scores on top. Build the crowd graph first
because it has no download dependency, then layer OSM in.

**Derived features:**

| Feature | From | Feeds |
|---|---|---|
| `curvature_geo` | way geometry heading deltas | fun (where crowd data is thin) |
| `junction_density` | junction nodes per km | flow — the brief's "clear road view" green flag |
| `road_class` | `highway=*` | scenic penalty for motorway; "inner city" red flag |
| `surface_quality` | `surface=asphalt/gravel/…` | risk; hard exclusion for `dirtRoads` when the rider forbids them |
| `maxspeed` | tag | the 50–120 km/h sweet-spot band |
| `tunnel_share` | `tunnel=yes` | scenic penalty — no view in a tunnel |
| `bridge`, `viewpoint`, `water_prox`, `forest_share` | tags and polygons | scenic |
| `construction` | `highway=construction` | hard edge exclusion |

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
| BMW crowd lake (85,699 trips) | — | static | lean, curviness, observed speed, ABS, congestion prior, **exposure denominator** | fun, risk, traffic, confidence |
| BMW personal trips | — | static | revealed preference, lean envelope, skill vector | weights, ceilings, learning |
| **OpenStreetMap** | no | static | road class, geometric curvature, surface, junctions, POIs | fun, scenic, risk, exclusions |
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
