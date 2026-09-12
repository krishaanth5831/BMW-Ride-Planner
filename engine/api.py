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

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import weather as weather_mod
from engine.profile import build_profile
from engine.router import Graph, joyride
from engine.scoring import Scorer
from precompute.build_graph import node_center

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


def _plan_from_paths(paths: list[str], name: str) -> dict:
    graph = get_graph()
    profile = build_profile(paths, graph.segments, name=name)
    scorer = Scorer(graph.segments, profile)

    ctx = profile["context"]
    origin = ctx["origin"]
    origin_node = graph.nearest_node(origin["lat"], origin["lon"])
    if origin_node is None:
        raise HTTPException(422, "could not place the rider's start point on the graph")

    wx = weather_mod.fetch(origin["lat"], origin["lon"])
    routes = joyride(
        graph, scorer, origin_node,
        minutes=ctx["median_ride_minutes"],
        alpha=3.0,
        weather=wx.get("risk", 0.0),
        weather_factor=wx.get("capability_factor", 1.0),
    )
    if not routes:
        raise HTTPException(
            422,
            "no route found from this rider's usual start point -- their rides may "
            "fall outside the region the crowd graph covers",
        )

    onode = node_center(origin_node)
    labels = ["Balanced", "Alternative", "Long way round"]
    for i, r in enumerate(routes):
        r["label"] = labels[i] if i < len(labels) else f"Option {i + 1}"

    profile.pop("_cells", None)
    return {
        "profile": profile,
        "weather": wx,
        "origin": {"lat": onode[0], "lon": onode[1],
                   "snapped_from": origin, "node": origin_node},
        "routes": routes,
        "explain": {
            "why_origin": "the start point of the rider's own most frequent trips",
            "why_duration": f"median of their {profile['n_trips']} recorded rides",
            "weights": profile["taste"]["weights"],
            "style_ratio": profile["capability"]["style_ratio"],
            "style_basis_cells": profile["capability"]["style_basis_cells"],
        },
    }


@app.post("/api/plan/upload")
async def plan_upload(files: list[UploadFile] = File(...)):
    """Upload one or more rider CSVs, or a zip of them, and get routes back."""
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
        return _plan_from_paths(csvs, name=f"upload:{name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/plan/example/{rider}")
def plan_example(rider: str):
    """Convenience path for the bundled example riders (A / B / C)."""
    folder = os.path.join(DATASET, f"exampleUser{rider.upper()}", "recordedTrips")
    if not os.path.isdir(folder):
        raise HTTPException(404, f"example rider {rider} not found at {folder}")
    csvs = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.endswith(".csv") and not f.startswith(".")
    )
    return _plan_from_paths(csvs, name=f"exampleUser{rider.upper()}")


@app.get("/api/segments")
def segments(limit: int = 4000):
    """The crowd layer, for the map underlay."""
    g = get_graph()
    out = sorted(g.segments, key=lambda s: -(s.get("curviness") or 0))[:limit]
    return {
        "segments": [
            {
                "seg_id": s["seg_id"],
                "geometry": [[p[0], p[1]] for p in s["geometry"]],
                "curviness": round(s.get("curviness") or 0, 1),
                "speed_mean": round(s.get("speed_mean") or 0, 1),
                "n_trips": int(s.get("n_trips") or 0),
            }
            for s in out
        ]
    }


if os.path.isdir(WEB):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB, "index.html"))

    app.mount("/static", StaticFiles(directory=WEB), name="static")
