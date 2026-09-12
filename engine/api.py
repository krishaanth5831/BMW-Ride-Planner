"""FastAPI app: upload rider telemetry, get a route back.

No per-ride preferences are asked for. Everything -- where the ride starts, how
long it runs, what counts as good -- is derived from the uploaded data
(plan/ALGORITHM.md section 6, channel 3).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import weather as weather_mod
from engine.profile import build_profile
from engine.router import Graph, plan_alternatives, plan_heatmap, plan_loop
from engine.scoring import Scorer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def _in_bbox(point: tuple[float, float], bbox: list | tuple) -> bool:
    lat, lon = point
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
