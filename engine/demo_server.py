"""Zero-dependency server for the dataset-free BMW Ride Planner demo.

Run from the repository root:

    python3 -m engine.demo_server

It serves the existing /ride UI and calculates routes on the committed OSM
fixture. No FastAPI, pip packages, BMW telemetry, or generated data directory
is required.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engine import cell_osm_router as roads
from engine import cell_router as cells
from engine import demo
from engine import rideworthy
from engine import scenic
from engine import terrain


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


@lru_cache(maxsize=1)
def runtime():
    cell_graph = cells.CellGraph(demo.NEUTRAL_CELL_GRAPH)
    road_graph = roads.load_blob(demo.NEUTRAL_CELL_GRAPH)
    scenic_index = scenic.ScenicIndex(road_graph, scenic.load_pois())
    return cell_graph, road_graph, scenic_index


def profile_payload(rider: str) -> dict:
    profile = demo.sample_profile(rider)
    profile.pop("ridden_squares", None)
    profile["rider"] = rider
    return profile


def weather_payload(rider: str) -> dict:
    profile = demo.sample_profile(rider)
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
    profile = demo.sample_profile(rider)
    minutes = max(30.0, min(float(body.get("minutes") or 150), 360.0))
    origin_ll = body.get("origin") or [profile["home"]["lat"], profile["home"]["lon"]]
    if not (roads.BBOX[0] <= origin_ll[0] <= roads.BBOX[2]
            and roads.BBOX[1] <= origin_ll[1] <= roads.BBOX[3]):
        raise ValueError("origin is outside the committed Bavaria road fixture")

    cell_graph, graph, index = runtime()
    rider_model = cells.Rider(cell_graph, ridden=None,
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
            "reason": planned.get("reason"), "demo_mode": True,
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
        "excluded_segments": 0, "total_segments": len(graph.segments),
        "demo_mode": True,
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/ride"):
                return self.file_response(os.path.join(WEB, "ride.html"))
            if path == "/api/demo/status":
                return self.json_response({"ok": True, "mode": "lightweight-demo",
                                           "dataset_required": False})
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
        if urlparse(self.path).path != "/api/ride/suggest":
            return self.json_response({"detail": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            return self.json_response(suggest_payload(body))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.json_response({"detail": str(exc)}, 400)
        except Exception as exc:
            return self.json_response({"detail": f"route calculation failed: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the dataset-free BMW demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Lightweight BMW demo: http://{args.host}:{args.port}/ride", flush=True)
    print("No BMW telemetry is loaded; sample values are labeled in the UI.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
