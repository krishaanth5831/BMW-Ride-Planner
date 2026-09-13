# Demo script

Two surfaces, same engine.

| | |
|---|---|
| `/ride` | the product a customer sees |
| `/cells` | the engineering view: both routers, the raw graph, every number |

## Before the room

```bash
python3 -m engine.demo_server
```

That starts a lightweight, dataset-free version using real cached OSM roads and
clearly labeled sample profiles. It has no third-party Python dependencies. For
the telemetry-backed room demo, install `requirements.txt` plus `duckdb`, build
`data/cell_graph.json`, and run the FastAPI app with uvicorn.

`data/` is gitignored, so the real crowd graph has to be built once on any new
machine. Everything else (road tiles, scenic points) is cached in `fixtures/`
and needs no network. The forecast is live but falls back to its own cache, so
the demo survives a dead wifi.

## The five minutes

**1. Open `/ride`. Say nothing for a second.** The page has already decided
there is a ride worth taking. No form was filled in.

> "Today, 07:00 to 13:00. 17 degrees, 8% chance of rain, gusts 24 km/h."

That is the notification. It came from scoring every forecast hour for riding
specifically, then merging the good ones. Rain is weighted first because it is
the only one that ends a ride outright.

**2. Scroll to the profile before planning anything.**

> "Roadster. Intermediate. Lean used 16.7 degrees, safety ceiling 19.3.
> Typical ride 39 minutes."

None of that was asked for. Bike family comes from revs per km/h, the ceiling
from the angle this rider actually uses while moving, home from where their
rides start.

**3. Press plan.** The route goes south out of Munich to Starnberger See and
back. Point at three things on the card:

- **the stops**, which are named places, not a radius
- **48% in town**, down from 88% before the city-escape logic
- **"1,116 roads were left out because the crowd rides them harder than your
  19.3 degree lean ceiling"**

**4. Switch to Rider C and plan again.** Ceiling 26.5 degrees. The same request
now reaches **Tegernsee**, 123 km. Nothing else changed.

> "Safety here is a hard exclusion, not a penalty. A soft penalty can always be
> overwhelmed by a big enough fun bonus. This cannot: those roads are not in
> this rider's map at all."

That single comparison is the strongest thirty seconds in the demo.

**5. If there is time, open `/cells`** and switch Geometry to "Square lattice".
The same algorithm drawn on morton squares instead of roads: 91 km of staircase
against 74 km of road, 33% of turns over 90 degrees against 0.1%.

## The one honest gap

There is no push channel. `/api/ride/windows` returns exactly the payload a
notification would carry and labels its own delivery as in-app. Wiring it to
APNs or FCM is a day; pretending it was wired would have been a lie on stage.

## Numbers worth having ready

| | |
|---|---|
| Trips ingested | 32,450 (8.8M samples) |
| Crowd graph | 117,929 squares, 122,314 observed transitions |
| Road network | 65,718 OSM segments, 64% carrying crowd data |
| Scenic points | 635 (53 lakes, 13 passes, 243 peaks, 326 viewpoints) |
| Cost inversion | fast dull road loses to slow good road at alpha 4.8 |
| Routing time | under 2 s per route, pure Python |
