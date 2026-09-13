"""Zero-dependency server for the BMW Ride Planner product UI.

Run from the repository root:

    python3 -m engine.demo_server

It serves /ride and routes on the committed OSM fixture, using precomputed BMW
telemetry and complete rider profiles when configured. Otherwise its sample
values are labeled. No FastAPI or third-party packages are required.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engine import cell_osm_router as roads
from engine import cell_router as cells
from engine import demo
from engine import rideworthy
from engine import scenic
from engine import terrain
from engine import ride_data
from engine.cache import lock_for


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


def runtime():
    # lru_cache alone can execute the first expensive build multiple times
    # when the POI request, warmup and route request arrive together.
    with lock_for("demo-runtime"):
        return _runtime()


@lru_cache(maxsize=1)
def _runtime():
    blob = ride_data.crowd_blob()
    cell_graph = cells.CellGraph(blob)
    road_graph = roads.load_blob(blob)
    scenic_index = scenic.ScenicIndex(road_graph, scenic.load_pois())
    return cell_graph, road_graph, scenic_index


def profile_payload(rider: str) -> dict:
    profile = dict(ride_data.profile(rider))
    profile.pop("ridden_squares", None)
    profile["rider"] = rider
    return profile


def weather_payload(rider: str) -> dict:
    profile = ride_data.profile(rider)
    payload = rideworthy.windows(profile["home"]["lat"], profile["home"]["lon"],
                                  days=7)
    if not payload.get("available"):
        payload = demo.sample_weather()
        payload["fallback_reason"] = "Open-Meteo was unavailable"
    display_windows = payload.get("checked_windows") or payload["windows"]
    payload["windows"] = display_windows[:5]
    for window in payload["windows"]:
        window["headline"] = rideworthy.headline(window)
        usable_hours = ((window.get("best_period") or {}).get("hours")
                        or (4 if window.get("availability", "full") == "full" else 0))
        window["suggested_minutes"] = int(min(usable_hours * 60, 150))
    payload.update({
        "home": profile["home"], "rider": rider,
        "home_is_sample": bool(profile.get("demo_mode")),
        "delivery": "in-app demo",
        "demo_mode": payload.get("source") == "bundled sample",
        "weather_provider": ("Open-Meteo" if payload.get("source") in ("live", "cache")
                             else "bundled sample"),
    })
    return payload


def suggest_payload(body: dict) -> dict:
    rider = str(body.get("rider") or "A").upper()
    if rider not in demo.SAMPLE_PROFILES:
        raise ValueError("unknown rider; use A, B, or C")
    profile = ride_data.profile(rider)
    minutes = max(30.0, min(float(body.get("minutes") or 150), 360.0))
    origin_ll = body.get("origin") or [profile["home"]["lat"], profile["home"]["lon"]]
    if not (roads.BBOX[0] <= origin_ll[0] <= roads.BBOX[2]
            and roads.BBOX[1] <= origin_ll[1] <= roads.BBOX[3]):
        raise ValueError("origin is outside the committed Bavaria road fixture")

    cell_graph, graph, index = runtime()
    rider_model = cells.Rider(cell_graph, ridden=profile.get("ridden_squares"),
                               lean_p95=profile["lean_ceiling"])
    cell_cost = cells.Cost(cell_graph, rider_model,
                           float(body.get("weather") or 0.0))
    cost = roads.RoadCost(
        graph, cell_cost, escape=bool(body.get("escape", True)),
        rider_style=rider, rider_speed_kmh=profile["speed_mean_kmh"])
    origin = graph.nearest_node(*origin_ll)
    planned = scenic.plan_scenic_loop(
        graph, cost, index, origin, minutes,
        alpha=float(body.get("alpha", 3.0)),
        max_stops=int(body.get("max_stops", 3)),
        south_bias=body.get("south_bias"),
        rider_id=rider,
        rider_profile=profile,
        seed=body.get("seed"),
    )

    if not planned["routes"]:
        loops = roads.joyride(graph, cost, origin, minutes,
                              alpha=float(body.get("alpha", 3.0)))
        loops = scenic.rank_loop_fallbacks(
            loops, index, rider, minutes, profile, body.get("seed"))
        if loops:
            terrain.enrich_route(loops[0], body.get("terrain", True) is not False)
        keys = ("label", "bearing", "coords", "kpis", "roads",
                "value_thirds", "urban_share", "overlap", "visited",
                "personalization", "spot_search")
        return {
            "rider": rider, "minutes": minutes, "kind": "loop",
            "origin": list(graph.node_pos(origin)),
            "reason": planned.get("reason"),
            "demo_mode": bool(profile.get("demo_mode") or not cell_graph.cells),
            "routes": [{k: route[k] for k in keys if k in route} for route in loops],
        }

    route = planned["routes"][0]
    terrain.enrich_route(route, body.get("terrain", True) is not False)
    keys = ("label", "coords", "kpis", "roads", "stops", "legs",
            "visited", "value_thirds", "urban_share", "personalization",
            "spot_search")
    return {
        "rider": rider, "minutes": minutes, "kind": "scenic-chain",
        "origin": list(graph.node_pos(origin)),
        "origin_urban": round(graph.urban_of_node(origin), 2),
        "lean_ceiling": profile["lean_ceiling"],
        "bike_class": profile["bike_class"],
        "excluded_segments": sum(cost.excluded(s) for s in graph.segments),
        "total_segments": len(graph.segments),
        "demo_mode": bool(profile.get("demo_mode") or not cell_graph.cells),
        "routes": [{k: route[k] for k in keys if k in route}],
    }


def map_config() -> dict:
    return {"default": "osm", "layers": [
        {"id": "osm", "name": "OSM",
         "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
         "attribution": "&copy; OpenStreetMap contributors", "dark": False},
    ]}


class Handler(BaseHTTPRequestHandler):
    server_version = "BMWDemo/1.0"

    def json_response(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def file_response(self, path: str):
        if not os.path.isfile(path):
            return self.json_response({"detail": "not found"}, 404)
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/ride"):
                return self.file_response(os.path.join(WEB, "ride.html"))
            if path == "/api/demo/status":
                return self.json_response(ride_data.status())
            if path == "/api/mapconfig":
                return self.json_response(map_config())
            if path.startswith("/api/ride/profile/"):
                rider = path.rsplit("/", 1)[-1].upper()
                if rider not in demo.SAMPLE_PROFILES:
                    raise ValueError("unknown rider; use A, B, or C")
                return self.json_response(profile_payload(rider))
            if path == "/api/ride/windows":
                rider = parse_qs(parsed.query).get("rider", ["A"])[0].upper()
                if rider not in demo.SAMPLE_PROFILES:
                    raise ValueError("unknown rider; use A, B, or C")
                return self.json_response(weather_payload(rider))
            if path == "/api/ride/pois":
                index = runtime()[2]
                pois = sorted(index.pois, key=lambda item: -item["weight"])
                requested = int(parse_qs(parsed.query).get("limit", ["1000"])[0])
                limit = max(1, min(requested, 1000))
                payload = [{"name": poi["name"], "kind": poi["kind"],
                            "lat": poi["target_lat"], "lon": poi["target_lon"],
                            "weight": poi["weight"],
                            "objective_scenic": poi["objective_scenic"],
                            "location_source": poi["target_source"],
                            "rider_style_owner": poi["rider_style_owner"],
                            "style_features": poi["style_features"],
                            "rider_fit": poi["rider_fit"],
                            "rider_rank": poi["rider_rank"]}
                           for poi in pois[:limit]]
                return self.json_response({
                    "total": len(pois),
                    "rating_scale": {"min": 1, "max": 10},
                    "rating_basis": "bundled POI and OSM road attributes; not telemetry",
                    "pois": payload,
                })
            return self.json_response({"detail": "not found"}, 404)
        except (ValueError, TypeError) as exc:
            return self.json_response({"detail": str(exc)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/ride/suggest", "/api/ride/terrain"):
            return self.json_response({"detail": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 2_000_000:
                raise ValueError("request is too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if path == "/api/ride/terrain":
                return self.json_response(terrain.payload(body))
            return self.json_response(suggest_payload(body))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.json_response({"detail": str(exc)}, 400)
        except Exception as exc:
            return self.json_response({"detail": f"route calculation failed: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the BMW ride UI, using real telemetry when configured")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dataset", help="Full datasetHackathon directory")
    parser.add_argument("--rider-a", help="Optional recordedTrips directory for rider A only")
    parser.add_argument("--cell-graph", help="Precomputed crowd graph JSON")
    parser.add_argument("--warmup", action="store_true", help="Prepare graphs and profiles before announcing readiness")
    args = parser.parse_args()
    for key, value in (("BMW_DATASET", args.dataset), ("BMW_RIDER_A", args.rider_a),
                       ("BMW_CELL_GRAPH", args.cell_graph)):
        if value:
            os.environ[key] = value
    if args.warmup:
        print("Preparing road graph and complete rider profiles (cached after the first run)...", flush=True)
        runtime()
        state = ride_data.status()
        print(f"Crowd: {state['crowd_source']} ({state['crowd_cells']:,} cells)", flush=True)
        for rider, info in state["riders"].items():
            print(f"Rider {rider}: {info['source']}, {info['trips']} trips", flush=True)
        print("Preparing weather forecasts (live or explicitly labeled fallback)...", flush=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for rider, payload in zip(ride_data.RIDERS, pool.map(weather_payload, ride_data.RIDERS)):
                print(f"Rider {rider} weather: {payload['source']}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"BMW Ride Planner ready: http://{args.host}:{args.port}/ride", flush=True)
    print("Data provenance: /api/demo/status (sample values remain explicitly labeled).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
