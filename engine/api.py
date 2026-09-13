"""FastAPI app: upload rider telemetry, get a route back.

No per-ride preferences are asked for. Everything -- where the ride starts, how
long it runs, what counts as good -- is derived from the uploaded data
(plan/ALGORITHM.md section 6, channel 3).
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import weather as weather_mod
from engine import demo as demo_data
from engine import ride_data
from engine.cache import lock_for
from engine.profile import build_profile
from engine.router import Graph, plan_alternatives, plan_heatmap, plan_loop
from engine.scoring import Scorer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path: str = None) -> None:
    """Read .env without adding a dependency. Real environment always wins."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()
DATA = os.path.join(ROOT, "data")
WEB = os.path.join(ROOT, "web")
DATASET = os.environ.get(
    "BMW_DATASET", os.path.expanduser("~/Desktop/BMW/exd_download/datasetHackathon")
)

app = FastAPI(title="BMW Ride Planner", version="0.1.0")

_state: dict = {"segments": None, "graph": None}


def get_graph() -> Graph:
    if _state["graph"] is None:
        seg_path = os.path.join(DATA, "segments.json")
        graph_path = os.path.join(DATA, "graph.json")
        if not os.path.exists(seg_path):
            raise HTTPException(
                503,
                "No road graph yet. Run:  python -m precompute.build_graph "
                "--shards 'trips-samples-*/*' --out data",
            )
        with open(seg_path) as fh:
            segments = json.load(fh)
        with open(graph_path) as fh:
            g = json.load(fh)
        _state["segments"] = segments
        _state["graph"] = Graph(segments, g["adjacency"])
        _state["meta"] = g["meta"]
        from precompute.build_osm_graph import build_index
        _state["index"] = build_index(segments)
    return _state["graph"]


@app.get("/api/health")
def health():
    try:
        g = get_graph()
        return {
            "ok": True,
            "segments": len(g.segments),
            "meta": _state.get("meta", {}),
            "example_riders": sorted(
                d for d in ("exampleUserA", "exampleUserB", "exampleUserC")
                if os.path.isdir(os.path.join(DATASET, d, "recordedTrips"))
            ),
        }
    except HTTPException as e:
        return JSONResponse({"ok": False, "detail": e.detail}, status_code=503)


def _in_bbox(point: tuple[float, float], bbox: list | tuple | None = None) -> bool:
    lat, lon = point
    if bbox is None:
        bbox = _croads.BBOX
    south, west, north, east = map(float, bbox)
    return south <= lat <= north and west <= lon <= east


def _plan(paths: list[str], name: str, mode: str, minutes: float | None,
          dest: tuple[float, float] | None, start_hour: float | None = None,
          origin_override: tuple[float, float] | None = None,
          radius_km: float = 100.0) -> dict:
    graph = get_graph()
    meta = _state.get("meta", {})
    if mode == "heatmap" and (not dest or not origin_override):
        raise HTTPException(422, "heatmap mode requires origin_lat/lon and dest_lat/lon")
    if origin_override and not _in_bbox(origin_override, meta.get("bbox", [])):
        raise HTTPException(422, {"message": "origin is outside the covered graph",
                                  "meta": {"bbox": meta.get("bbox")}})
    if dest and not _in_bbox(dest, meta.get("bbox", [])):
        raise HTTPException(422, {"message": "destination is outside the covered graph",
                                  "meta": {"bbox": meta.get("bbox")}})

    profile = build_profile(paths, graph.segments, name=name,
                            index=_state.get("index"))

    ctx = profile["context"]
    # Point A: whatever the user clicked, else the rider's own usual start.
    if origin_override:
        origin = {"lat": origin_override[0], "lon": origin_override[1]}
        origin_source = "chosen on the map"
    else:
        origin = ctx["origin"]
        origin_source = "the rider's own most frequent trip origin"
    origin_node = graph.nearest_node(origin["lat"], origin["lon"], require_degree=2)
    if origin_node is None:
        raise HTTPException(422, "could not place the rider's start point on the road network")

    # Duration is the rider's own median ride unless they asked for one.
    minutes = float(minutes) if minutes else float(ctx["median_ride_minutes"])
    minutes = max(15.0, min(minutes, 480.0))

    # Departure time drives scenic(t). Default to the hour this rider usually
    # sets off at, taken from their own trip history.
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=2))          # Munich, CEST
    now = _dt.datetime.now(tz)
    hour = int(ctx["usual_start_hour"]) if start_hour is None else int(start_hour)
    depart = now.replace(hour=max(0, min(23, hour)), minute=0, second=0, microsecond=0)
    wx = weather_mod.fetch(origin["lat"], origin["lon"])
    weather_factor = wx.get("capability_factor", 1.0)
    profile["lean_ceiling"] = round(
        float(profile.get("lean_ceiling") or 0.0) * weather_factor, 2)
    profile["capability"]["lean_ceiling"] = profile["lean_ceiling"]
    scorer = Scorer(graph.segments, profile)
    sun_ctx = scorer.set_time(depart, origin["lat"], origin["lon"])
    wargs = dict(weather=wx.get("risk", 0.0), weather_factor=weather_factor)

    if mode == "ab" or mode == "heatmap":
        if not dest:
            raise HTTPException(400, "point-to-point mode needs a destination")
        goal = graph.nearest_node(dest[0], dest[1], require_degree=2,
                                  component=graph.component_of(origin_node))
        if goal is None:
            raise HTTPException(422, {"message": "destination is not connected to the origin in the covered region",
                                      "meta": {"bbox": _state["meta"].get("bbox")}})
        if goal == origin_node:
            raise HTTPException(422, "destination is the same place as the start")
        if mode == "heatmap":
            sampled = plan_heatmap(graph, scorer, origin_node, goal,
                                   radius_km=radius_km, n=30,
                                   highway_avoidance=1.0,
                                   twist_avoidance=2.5,
                                   traffic_avoidance=2.0, **wargs)
            routes = sampled["routes"]
            heatmap = sampled["heatmap"]
            heatmap_meta = sampled["meta"]
        else:
            routes = plan_alternatives(graph, scorer, origin_node, goal, n=6, **wargs)
            heatmap = None
            heatmap_meta = None
        dest_pos = graph.node_pos(goal)
    else:
        heatmap = None
        heatmap_meta = None
        routes = plan_loop(graph, scorer, origin_node, minutes, **wargs)
        dest_pos = None

    if not routes:
        raise HTTPException(
            422,
            "no route found. The road network covers "
            f"{_state['meta'].get('bbox')}; this rider's usual start point or the "
            "destination may fall outside it.")

    olat, olon = graph.node_pos(origin_node)
    profile.pop("_cells", None)
    response = {
        "mode": mode,
        "requested_minutes": minutes,
        "profile": profile,
        "weather": wx,
        "sun": sun_ctx,
        "origin": {"lat": olat, "lon": olon, "snapped_from": origin},
        "destination": ({"lat": dest_pos[0], "lon": dest_pos[1]} if dest_pos else None),
        "routes": routes,
        "explain": {
            "value_term": ("personal heatmap ranking from scenic + fun scores; "
                            "highways are strongly avoided, not forbidden; "
                            "long uninterrupted roads are penalised and repeated "
                            "twists preferred; traffic pressure from BMW crowd "
                            "telemetry raises cost"
                            if mode == "heatmap"
                            else "scenic score only (legacy route mode)"),
            "scenic_is_time_dependent": (
                "scaled by available light, plus a golden-hour bonus for roads "
                "that actually face the low sun"),
            "scenic_inputs": ["curviness (crowd lean, blended with road geometry "
                              "by confidence)", "road class", "elevation",
                              "flow (no standstills)", "50-120 km/h share",
                              "tunnel penalty"],
            "profile_used_for": ["capability ceiling (hard exclusion)",
                                 "start point", "default duration"],
            "why_origin": origin_source,
            "graph": _state["meta"],
        },
    }
    if mode == "heatmap":
        response["heatmap"] = heatmap
        response["heatmap_meta"] = heatmap_meta
    return response


@app.post("/api/plan/upload")
async def plan_upload(
    files: list[UploadFile] = File(...),
    mode: str = Form("loop"),
    minutes: float | None = Form(None),
    dest_lat: float | None = Form(None),
    dest_lon: float | None = Form(None),
    start_hour: float | None = Form(None),
    origin_lat: float | None = Form(None),
    origin_lon: float | None = Form(None),
    radius_km: float = Form(100.0),
):
    """Upload rider CSVs (or a zip) and get routes back."""
    tmp = tempfile.mkdtemp(prefix="bmw_upload_")
    try:
        csvs: list[str] = []
        for f in files:
            dest = os.path.join(tmp, os.path.basename(f.filename or "upload"))
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            if dest.lower().endswith(".zip"):
                with zipfile.ZipFile(dest) as z:
                    z.extractall(tmp)
            elif dest.lower().endswith(".csv"):
                csvs.append(dest)
        for dirpath, _dirs, names in os.walk(tmp):
            for n in names:
                if n.lower().endswith(".csv") and not n.startswith("."):
                    p = os.path.join(dirpath, n)
                    if p not in csvs:
                        csvs.append(p)
        if not csvs:
            raise HTTPException(400, "no .csv telemetry found in the upload")
        name = os.path.basename(files[0].filename or "uploaded").rsplit(".", 1)[0]
        dest = (dest_lat, dest_lon) if dest_lat is not None and dest_lon is not None else None
        org = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
        return _plan(csvs, f"upload:{name}", mode, minutes, dest, start_hour, org,
                     radius_km)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/heatmap")
@app.post("/api/plan/heatmap")
async def heatmap_upload(
    files: list[UploadFile] = File(...),
    origin_lat: float = Form(...),
    origin_lon: float = Form(...),
    dest_lat: float = Form(...),
    dest_lon: float = Form(...),
    radius_km: float = Form(100.0),
    start_hour: float | None = Form(None),
):
    """Upload telemetry and build the X→Y route heatmap."""
    tmp = tempfile.mkdtemp(prefix="bmw_heatmap_")
    try:
        csvs: list[str] = []
        for f in files:
            path = os.path.join(tmp, os.path.basename(f.filename or "upload"))
            with open(path, "wb") as out:
                shutil.copyfileobj(f.file, out)
            if path.lower().endswith(".zip"):
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmp)
            elif path.lower().endswith(".csv"):
                csvs.append(path)
        for dirpath, _dirs, names in os.walk(tmp):
            for filename in names:
                if filename.lower().endswith(".csv") and not filename.startswith("."):
                    path = os.path.join(dirpath, filename)
                    if path not in csvs:
                        csvs.append(path)
        if not csvs:
            raise HTTPException(400, "no .csv telemetry found in the upload")
        name = os.path.basename(files[0].filename or "uploaded").rsplit(".", 1)[0]
        return _plan(csvs, f"upload:{name}", "heatmap", None,
                     (dest_lat, dest_lon), start_hour,
                     (origin_lat, origin_lon), radius_km)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/plan/example/{rider}")
def plan_example(rider: str, mode: str = "loop", minutes: float | None = None,
                 dest_lat: float | None = None, dest_lon: float | None = None,
                 start_hour: float | None = None,
                 origin_lat: float | None = None, origin_lon: float | None = None,
                 radius_km: float = 100.0):

    """Convenience path for the bundled example riders (A / B / C)."""
    folder = os.path.join(DATASET, f"exampleUser{rider.upper()}", "recordedTrips")
    if not os.path.isdir(folder):
        raise HTTPException(404, f"example rider {rider} not found at {folder}")
    csvs = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.endswith(".csv") and not f.startswith(".")
    )
    dest = (dest_lat, dest_lon) if dest_lat is not None and dest_lon is not None else None
    org = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    return _plan(csvs, f"exampleUser{rider.upper()}", mode, minutes, dest, start_hour,
                 org, radius_km)


@app.get("/api/segments")
def segments(limit: int = 4000):
    """The crowd layer, for the map underlay."""
    g = get_graph()
    from engine.scoring import Scorer as _S
    sc = _S(g.segments, None)
    scored = sorted(((sc.scenic(s), s) for s in g.segments), key=lambda x: -x[0])[:limit]
    return {
        "segments": [
            {
                "seg_id": s["seg_id"],
                "geometry": [[p[0], p[1]] for p in s["geometry"]],
                "scenic": round(v, 3),
                "name": s.get("name"),
                "highway": s.get("highway"),
            }
            for v, s in scored
        ]
    }


if os.path.isdir(WEB):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB, "index.html"))

    app.mount("/static", StaticFiles(directory=WEB), name="static")


# ---------------------------------------------------------------------------
# The original morton-cell router (see MORTON_ROUTER.md). Served alongside the
# segment planner rather than replacing it, so the two can be compared on the
# same machine at /cells.
# ---------------------------------------------------------------------------

from engine import cell_osm_router as _croads  # noqa: E402
from engine import cell_router as _cells  # noqa: E402

_cell_state: dict = {"graph": None, "roads": None, "blob": None, "demo": None}


def _cell_blob() -> dict:
    """Use real generated telemetry when present, else the neutral demo layer."""
    if _cell_state["blob"] is None:
        blob = ride_data.crowd_blob()
        _cell_state["demo"] = not bool(blob.get("cells"))
        _cell_state["blob"] = blob
    return _cell_state["blob"]


def get_cell_graph():
    with lock_for("api-cell-graph"):
        if _cell_state["graph"] is None:
            _cell_state["graph"] = _cells.CellGraph(_cell_blob())
    return _cell_state["graph"]


def get_road_graph():
    """OSM geometry with morton-cell costs. Built once; ~3 s and ~66k segments."""
    with lock_for("api-road-graph"):
        if _cell_state["roads"] is None:
            _cell_state["roads"] = _croads.load_blob(_cell_blob())
    return _cell_state["roads"]


def _cost(g, lean_p95: float, weather: float = 0.0):
    """A rider with no history yet: neutral weights, their own lean ceiling."""
    return _cells.Cost(g, _cells.Rider(g, ridden=None, lean_p95=lean_p95), weather)


@app.get("/api/cells/health")
def cells_health():
    g = get_cell_graph()
    return {
        "level": g.level,
        "squares": len(g.cells),
        "transitions": len(g.edges),
        "branching_pct": round(100 * g.branching(), 1),
        "region_speed_kmh": g.region_speed,
        "mode": "lightweight-demo" if _cell_state["demo"] else "telemetry",
    }


@app.get("/api/cells/coverage")
def cells_coverage(limit: int = 20000, min_trips: int = 3):
    """Where the crowd actually is. This is the map -- there is no other one."""
    g = get_cell_graph()
    rows = [c for c in g.cells.values() if (c.get("n_trips") or 0) >= min_trips]
    rows.sort(key=lambda c: -(c.get("n_trips") or 0))
    step = max(1, len(rows) // max(limit, 1))
    return {"total": len(rows), "points": [
        [c["lat"], c["lon"], c["n_trips"], c.get("lean_p50") or 0]
        for c in rows[::step][:limit]
    ]}


@app.post("/api/cells/route")
def cells_route(body: dict):
    """Mode 1: A -> B at three alphas.

    `engine` picks where the line is DRAWN, not how it is scored. Both run the
    identical distortion with the identical morton-cell terms:

      "roads" (default) -- OSM geometry and connectivity, cell scores
      "cells"           -- the pure lattice, square centres joined up
    """
    engine = body.get("engine", "roads")
    lean = float(body.get("lean_p95", 35.0))
    weather = float(body.get("weather", 0.0))
    cg = get_cell_graph()
    cost_cells = _cost(cg, lean, weather)

    if engine == "cells":
        start, goal = cg.nearest(*body["start"]), cg.nearest(*body["goal"])
        if start is None or goal is None:
            raise HTTPException(400, "could not snap those points to ridden squares")
        routes = _cells.plan_destination(cg, cost_cells, start, goal)
        snapped = ([cg.cells[start]["lat"], cg.cells[start]["lon"]],
                   [cg.cells[goal]["lat"], cg.cells[goal]["lon"]])
        keys = ("label", "alpha", "coords", "kpis")
    else:
        if not (_in_bbox(body["start"]) and _in_bbox(body["goal"])):
            s, w, n, e = _croads.BBOX
            raise HTTPException(
                400,
                f"Road routing covers {s}-{n} N, {w}-{e} E only -- the cached "
                f"Overpass tiles. Widen it deliberately with "
                f"`python3 -m precompute.fetch_osm --bbox ...`, not from a map "
                f"click. Or switch to the cell engine, which covers everywhere "
                f"the crowd rode.",
            )
        g = get_road_graph()
        cost = _croads.RoadCost(g, cost_cells,
                                escape=bool(body.get("escape", True)))
        start, goal = (g.nearest_node(*body["start"]), g.nearest_node(*body["goal"]))
        if start is None or goal is None:
            raise HTTPException(400, "could not snap those points to a road")
        routes = _croads.plan_destination(g, cost, start, goal)
        snapped = (list(g.node_pos(start)), list(g.node_pos(goal)))
        keys = ("label", "alpha", "coords", "kpis", "roads")

    return {
        "engine": engine,
        "start": snapped[0],
        "goal": snapped[1],
        "excluded_squares": sum(1 for c in cg.cells.values() if cost_cells.excluded(c)),
        "lean_p95": lean,
        "routes": [{k: r[k] for k in keys} for r in routes],
    }


@app.post("/api/cells/joyride")
def cells_joyride(body: dict):
    """Mode 2: X minutes from here, back to here."""
    engine = body.get("engine", "roads")
    lean = float(body.get("lean_p95", 35.0))
    minutes = float(body.get("minutes", 90))
    alpha = float(body.get("alpha", 3.0))
    cg = get_cell_graph()
    cost_cells = _cost(cg, lean, float(body.get("weather", 0.0)))

    if engine == "cells":
        start = cg.nearest(*body["origin"])
        if start is None:
            raise HTTPException(400, "could not snap that point to a ridden square")
        loops = _cells.joyride(cg, cost_cells, start, minutes, alpha=alpha)
        origin = [cg.cells[start]["lat"], cg.cells[start]["lon"]]
        keys = ("label", "bearing", "overlap", "coords", "kpis")
    else:
        if not _in_bbox(body["origin"]):
            raise HTTPException(400, "origin is outside the cached road tiles")
        g = get_road_graph()
        start = g.nearest_node(*body["origin"])
        cost = _croads.RoadCost(g, cost_cells,
                                escape=bool(body.get("escape", True)))
        loops = _croads.joyride(g, cost, start, minutes, alpha=alpha)
        origin = list(g.node_pos(start))
        keys = ("label", "bearing", "overlap", "coords", "kpis", "roads",
                "value_thirds", "urban_share", "turnaround_urban",
                "escaped_town")

    out = {"engine": engine, "origin": origin,
           "routes": [{k: r[k] for k in keys} for r in loops]}
    if engine != "cells":
        out["origin_urban"] = round(get_road_graph().urban_of_node(start), 2)
    return out


@app.get("/api/mapconfig")
def mapconfig():
    """Tile keys for the page.

    They live in .env, not in the committed HTML, and are read at request time
    so adding one needs no rebuild. Every keyed layer has a keyless fallback,
    so an absent or expired key degrades the basemap instead of breaking it.
    """
    stadia = os.environ.get("STADIA_API_KEY", "")
    geoapify = os.environ.get("GEOAPIFY_API_KEY", "")
    layers = []
    if stadia:
        layers += [
            {"id": "outdoors", "name": "Outdoors",
             "url": "https://tiles.stadiamaps.com/tiles/outdoors/{z}/{x}/{y}{r}.png?api_key=" + stadia,
             "attribution": "&copy; Stadia Maps &copy; OpenMapTiles &copy; OpenStreetMap",
             "dark": False},
            {"id": "terrain", "name": "Terrain",
             "url": "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png?api_key=" + stadia,
             "attribution": "&copy; Stadia Maps &copy; Stamen Design &copy; OpenStreetMap",
             "dark": False},
            {"id": "dark", "name": "Dark",
             "url": "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png?api_key=" + stadia,
             "attribution": "&copy; Stadia Maps &copy; OpenMapTiles &copy; OpenStreetMap",
             "dark": True},
        ]
    if geoapify:
        layers.append(
            {"id": "geoapify-dark", "name": "Dark (Geoapify)",
             "url": "https://maps.geoapify.com/v1/tile/dark-matter/{z}/{x}/{y}.png?apiKey=" + geoapify,
             "attribution": "&copy; Geoapify &copy; OpenMapTiles &copy; OpenStreetMap",
             "dark": True})
    layers.append(
        {"id": "osm", "name": "OSM", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
         "attribution": "&copy; OpenStreetMap contributors", "dark": False})
    return {"layers": layers, "default": layers[0]["id"]}


if os.path.isdir(WEB):
    @app.get("/cells")
    def cells_page():
        return FileResponse(os.path.join(WEB, "cells.html"))


# ---------------------------------------------------------------------------
# The product: weather notification -> scenic ride suggestion -> map.
# Everything below is what a customer sees; /cells stays as the engineering
# view of the same machinery.
# ---------------------------------------------------------------------------

from engine import cache as _cache  # noqa: E402
from engine import fog as _fog  # noqa: E402
from engine import replay as _replay  # noqa: E402
from engine import rider_profile as _profile  # noqa: E402
from engine import rideworthy as _weather  # noqa: E402
from engine import scenic as _scenic  # noqa: E402

_ride_state: dict = {"index": None, "profiles": {}, "rider_index": {}}

RIDERS = {"A": "exampleUserA", "B": "exampleUserB", "C": "exampleUserC"}


def get_scenic_index():
    with lock_for("api-scenic-index"):
        if _ride_state["index"] is None:
            _ride_state["index"] = _scenic.ScenicIndex(get_road_graph(),
                                                       _scenic.load_pois())
    return _ride_state["index"]


def get_profile(rider: str) -> dict:
    """Cached: a profile is ~40 CSV files and does not change between asks."""
    rider = rider.upper()
    if rider not in RIDERS:
        raise HTTPException(404, f"unknown rider {rider}; try one of {list(RIDERS)}")
    try:
        return ride_data.profile(rider)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/demo/status")
def demo_status():
    return ride_data.status()


@app.post("/api/ride/terrain")
def ride_terrain(body: dict):
    from engine import terrain
    try:
        return terrain.payload(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/ride/profile/{rider}")
def ride_profile(rider: str):
    p = dict(get_profile(rider))
    p.pop("ridden_squares", None)        # thousands of codes; not for the wire
    p["rider"] = rider.upper()
    return p


@app.get("/api/ride/windows")
def ride_windows(rider: str = "A", days: int = 7):
    """Today plus four daily sundown checks, including rejected days."""
    p = get_profile(rider)
    home = p.get("home") or {"lat": 48.137, "lon": 11.576}
    w = _weather.windows(home["lat"], home["lon"], days=days)
    if not w.get("available"):
        w = demo_data.sample_weather()
        w["fallback_reason"] = "Open-Meteo was unavailable"
    w["windows"] = (w.get("checked_windows") or w["windows"])[:5]
    for win in w["windows"]:
        win["headline"] = _weather.headline(win)
        usable_hours = ((win.get("best_period") or {}).get("hours")
                        or (4 if win.get("availability", "full") == "full" else 0))
        win["suggested_minutes"] = int(min(usable_hours * 60, 150))
    w["home"] = home
    w["home_is_sample"] = bool(p.get("demo_mode"))
    w["rider"] = rider.upper()
    # Delivery is the one piece not built: this is the payload a push would
    # carry, not a push.
    w["delivery"] = "in-app (no push channel wired)"
    w["demo_mode"] = w.get("source") == "bundled sample"
    w["weather_provider"] = ("Open-Meteo" if w.get("source") in ("live", "cache")
                             else "bundled sample")
    return w


@app.post("/api/ride/suggest")
def ride_suggest(body: dict):
    """A named ride: out of town, round the scenic points, home again."""
    rider = (body.get("rider") or "A").upper()
    p = get_profile(rider)
    minutes = float(body.get("minutes") or p.get("typical_ride_min") or 120)
    origin_ll = body.get("origin") or [p["home"]["lat"], p["home"]["lon"]]
    if not _in_bbox(origin_ll):
        raise HTTPException(400, "origin is outside the cached road tiles")

    g = get_road_graph()
    cg = get_cell_graph()
    rider_obj = _cells.Rider(cg, ridden=p.get("ridden_squares"),
                             lean_p95=p.get("lean_ceiling") or 35.0)
    weather_risk = float(body.get("weather") or 0.0)
    cost = _croads.RoadCost(g, _cells.Cost(cg, rider_obj, weather_risk),
                            escape=bool(body.get("escape", True)),
                            rider_style=rider,
                            rider_speed_kmh=p.get("speed_mean_kmh"))
    origin = g.nearest_node(*origin_ll)

    got = _scenic.plan_scenic_loop(
        g, cost, get_rider_index(rider), origin, minutes,
        alpha=float(body.get("alpha", 3.0)),
        max_stops=int(body.get("max_stops", 3)),
        south_bias=body.get("south_bias"),
        rider_id=rider if rider in _scenic.RIDER_TYPES else "A",
        rider_profile=p,
        seed=body.get("seed"))

    if not got["routes"]:
        # Fall back to the bearing joyride rather than showing nothing -- and
        # say which one the rider is looking at.
        #
        # Note the fallback is seed-INVARIANT, and cannot honestly be made
        # otherwise here: for rider A at 150 minutes exactly one loop clears
        # the 10% overlap cap, so there is nothing to choose between. Jittering
        # alpha was tried and changes nothing. Making this vary means either
        # relaxing that cap or fixing the chain, not adding randomness.
        loops = _croads.joyride(g, cost, origin, minutes,
                                alpha=float(body.get("alpha", 3.0)))
        loops = _scenic.rank_loop_fallbacks(
            loops, get_rider_index(rider),
            rider if rider in _scenic.RIDER_TYPES else "A",
            minutes, p, body.get("seed"))
        if loops:
            from engine import terrain as _terrain
            _terrain.enrich_route(loops[0], body.get("terrain", True) is not False)
        return {"rider": rider, "minutes": minutes, "kind": "loop",
                "origin": list(g.node_pos(origin)),
                "reason": got.get("reason"),
                "routes": [{k: r[k] for k in
                            ("label", "bearing", "coords", "kpis", "roads",
                             "value_thirds", "urban_share", "overlap", "visited",
                             "personalization", "spot_search")} for r in loops]}

    # How much of the road network this rider's own lean ceiling removed. It is
    # the difference between Starnberger See and Tegernsee for rider A, so the
    # product says it rather than quietly handing over a lesser ride.
    excluded = sum(1 for seg in g.segments if cost.excluded(seg))

    r = got["routes"][0]
    from engine import terrain as _terrain
    _terrain.enrich_route(r, body.get("terrain", True) is not False)
    return {
        "rider": rider, "minutes": minutes, "kind": "scenic-chain",
        "origin": list(g.node_pos(origin)),
        "origin_urban": round(g.urban_of_node(origin), 2),
        "lean_ceiling": p.get("lean_ceiling"),
        "bike_class": p.get("bike_class"),
        "excluded_segments": excluded,
        "total_segments": len(g.segments),
        "demo_mode": bool(p.get("demo_mode") or _cell_state["demo"]),
        "routes": [{k: r[k] for k in
                    ("label", "coords", "kpis", "roads", "stops", "legs",
                     "visited", "value_thirds", "urban_share",
                     "personalization", "spot_search")}],
    }


def _rider_cost(g, p, weather: float = 0.0, escape: bool = True):
    cg = get_cell_graph()
    return _croads.RoadCost(
        g, _cells.Cost(cg, _cells.Rider(cg, ridden=p.get("ridden_squares"),
                                        lean_p95=p.get("lean_ceiling") or 35.0),
                       weather),
        escape=escape)


def get_rider_index(rider: str):
    """Scenic POIs plus the good roads THIS rider has never ridden.

    The fog map's red roads are more useful as destinations than as an
    overlay: giving the planner a reason to go somewhere new is what stops it
    offering the same lakes every time. They are snapped through the same
    ScenicIndex as the Overpass POIs, so nothing downstream needs a special
    case -- only the candidate list gets longer and more personal.

    Cached per rider: snapping is a brute-force nearest-node sweep, and the
    answer only changes when the rider rides somewhere new.
    """
    rider = rider.upper()
    if rider not in _ride_state["rider_index"]:
        g = get_road_graph()
        base = get_scenic_index()
        p = get_profile(rider)
        ridden = _fog.ridden_segments(g, p.get("ridden_squares"))
        extra = _fog.unridden_pois(g, _rider_cost(g, p), ridden)
        idx = _scenic.ScenicIndex(g, extra)
        # ScenicIndex partitions the public POIs between the three rider
        # styles, so each rider only sees its own share. These are not public
        # POIs: they are derived from THIS rider's own coverage and belong to
        # them, so they are stamped accordingly instead of falling to the "A"
        # default and vanishing for everyone else.
        for item in idx.pois:
            item["rider_style_owner"] = rider
        merged = _scenic.ScenicIndex.__new__(_scenic.ScenicIndex)
        merged.g = g
        merged.pois = list(base.pois) + list(idx.pois)
        _ride_state["rider_index"][rider] = merged
    return _ride_state["rider_index"][rider]


@app.get("/api/ride/fog/{rider}")
def ride_fog(rider: str, targets: int = 8):
    """Which roads this rider has ridden, and the best ones they have not.

    Counted in road-kilometres rather than grid squares, per plan/UI.md
    section 5: "You have ridden 214 km of the 5,045 km down here" is a
    sentence a rider feels; a percentage of squares is not.
    """
    p = get_profile(rider)
    g = get_road_graph()
    cg = get_cell_graph()
    cost = _croads.RoadCost(
        g, _cells.Cost(cg, _cells.Rider(cg, ridden=p.get("ridden_squares"),
                                        lean_p95=p.get("lean_ceiling") or 35.0)))
    out = _fog.build(g, cost, p.get("ridden_squares"), n_targets=targets)
    out["rider"] = rider.upper()
    out["home"] = p.get("home")
    return out


@app.post("/api/ride/discover")
def ride_discover(body: dict):
    """Plan a loop that goes to a road this rider has never ridden.

    The fog map's whole point is to end in a ride. Same cost function, same
    escape-the-city behaviour, one forced stop: the road they picked.
    """
    rider = (body.get("rider") or "A").upper()
    p = get_profile(rider)
    minutes = float(body.get("minutes") or p.get("typical_ride_min") or 150)
    target = body.get("target")
    if not target or len(target) != 2:
        raise HTTPException(400, "target must be [lat, lon]")
    origin_ll = body.get("origin") or [p["home"]["lat"], p["home"]["lon"]]
    if not (_in_bbox(origin_ll) and _in_bbox(target)):
        raise HTTPException(400, "origin or target is outside the cached road tiles")

    g = get_road_graph()
    cg = get_cell_graph()
    cost = _croads.RoadCost(
        g, _cells.Cost(cg, _cells.Rider(cg, ridden=p.get("ridden_squares"),
                                        lean_p95=p.get("lean_ceiling") or 35.0)),
        escape=bool(body.get("escape", True)))
    origin = g.nearest_node(*origin_ll)
    stop_node = g.nearest_node(*target)
    if origin is None or stop_node is None:
        raise HTTPException(400, "could not snap to a road")

    # Pinned as the ANCHOR of a normal scenic chain, not routed to and back.
    # An out-and-back to one road retraces itself, which the chain builder
    # rejects as a destination on a stick -- correctly. Going through it on a
    # loop is both a better ride and the thing that passes that guard.
    name = body.get("name") or "your new road"
    got = _scenic.plan_scenic_loop(
        g, cost, get_rider_index(rider), origin, minutes,
        alpha=float(body.get("alpha", 3.0)),
        max_stops=int(body.get("max_stops", 2)),
        rider_id=rider if rider in _scenic.RIDER_TYPES else "A",
        rider_profile=p,
        must_include=(target[0], target[1], name),
        # The rider picked this road, so a longer shared stretch is acceptable
        # here in a way it would not be for a ride the planner invented. Still
        # capped, so it cannot degenerate into out-and-back down one road.
        retrace=(14_000.0, 0.22))
    if not got or not got.get("routes"):
        raise HTTPException(
            404, got.get("reason")
            or "That road will not fit inside this much time. Try longer.")

    r = got["routes"][0]
    # The chain builder falls through to another anchor when the pinned one
    # cannot make a loop, which is right for a joy ride and wrong here: the
    # rider asked for THAT road. Offering a different one without saying so
    # would be the planner quietly ignoring them.
    reached = min(
        (_scenic.haversine_m((target[0], target[1]), (c[0], c[1]))
         for c in r["coords"][::3]), default=1e9)
    if reached > 1500.0:
        raise HTTPException(
            404,
            f"No loop through {name} fits in {minutes:.0f} minutes without "
            f"riding the same road both ways. Give it longer.")
    return {
        "rider": rider, "minutes": minutes, "kind": "discover",
        "origin": list(g.node_pos(origin)),
        "lean_ceiling": p.get("lean_ceiling"),
        "excluded_segments": sum(1 for seg in g.segments if cost.excluded(seg)),
        "total_segments": len(g.segments),
        "routes": [{k: r[k] for k in
                    ("label", "coords", "kpis", "roads", "stops", "legs",
                     "visited", "value_thirds", "urban_share")}],
    }


@app.get("/api/ride/replay/{rider}")
def ride_replay(rider: str):
    """A ride this rider actually did, read back off the bike.

    Nothing here touches the planner. It is the other half of the loop: the
    plan says where to go, this says what happened, and every figure in it was
    measured rather than modelled. Lean angle is the one a phone cannot give
    you, which is the whole reason a post-ride summary is worth showing.
    """
    rider = rider.upper()
    if rider not in RIDERS:
        raise HTTPException(404, f"unknown rider {rider}")
    key = "replay_" + rider
    if key not in _ride_state:
        folder = os.path.join(DATASET, RIDERS[rider], "recordedTrips")
        if not os.path.isdir(folder):
            raise HTTPException(503, f"rider data not found at {folder}")
        # Same disk cache as the profiles: picking the best ride means parsing
        # 40 trips, and the answer only changes when the trip files do.
        trips = _replay.list_trips(folder)
        got = _cache.derived_json(
            f"replay-{rider}.json",
            _cache.signature(trips, "replay-v1"),
            lambda: _replay.best_trip(folder))
        if not got:
            raise HTTPException(404, "no usable recorded ride for this rider")
        _ride_state[key] = got
    return {**_ride_state[key], "rider": rider,
            "source": "recorded telemetry, not simulated"}


@app.get("/api/ride/pois")
def ride_pois(limit: int = 400):
    idx = get_scenic_index()
    pois = sorted(idx.pois, key=lambda p: -p["weight"])[:limit]
    return {"total": len(idx.pois),
            "pois": [{"name": p["name"], "kind": p["kind"],
                       "lat": p["target_lat"], "lon": p["target_lon"],
                       "weight": p["weight"],
                       "objective_scenic": p["objective_scenic"],
                       "location_source": p["target_source"],
                       "rider_style_owner": p["rider_style_owner"],
                       "style_features": p["style_features"],
                       "rider_fit": p["rider_fit"],
                       "rider_rank": p["rider_rank"]}
                     for p in pois]}


if os.path.isdir(WEB):
    @app.get("/ride")
    def ride_page():
        return FileResponse(os.path.join(WEB, "ride.html"))
