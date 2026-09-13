"""Exercise both HTTP adapters, including deferred elevation and real profiles."""

from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from engine import demo, demo_server, ride_data, terrain


class PresentationHTTPTests(unittest.TestCase):
    def test_simultaneous_runtime_build_is_single_flight(self):
        demo_server._runtime.cache_clear()
        barrier = threading.Barrier(4)

        def load():
            barrier.wait()
            return demo_server.runtime()

        def slow_graph(_blob):
            time.sleep(.02)
            return object()

        try:
            with patch.object(ride_data, "crowd_blob", return_value=demo.NEUTRAL_CELL_GRAPH), \
                 patch.object(demo_server.cells, "CellGraph"), \
                 patch.object(demo_server.roads, "load_blob", side_effect=slow_graph) as build, \
                 patch.object(demo_server.scenic, "ScenicIndex"):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(lambda _: load(), range(4)))
                self.assertEqual(build.call_count, 1)
                self.assertTrue(all(result is results[0] for result in results))
        finally:
            demo_server._runtime.cache_clear()

    def test_stdlib_server_routes_and_terrain_endpoint(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), demo_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for path in ("/ride", "/api/demo/status", "/api/ride/profile/A", "/api/mapconfig"):
                with urllib.request.urlopen(base + path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
            request = urllib.request.Request(base + "/api/ride/terrain", data=json.dumps({
                "coords": [[48, 11], [48.01, 11.01]]}).encode(),
                headers={"Content-Type": "application/json"})
            with patch.object(terrain, "elevation_stats", return_value={
                    "elevation_available": True, "elev_gain_m": 120}):
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["elev_gain_m"], 120)
            request.data = b'{"coords": []}'
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fastapi_keeps_legacy_and_new_routes(self):
        try:
            from fastapi.testclient import TestClient
            from engine.api import app, _in_bbox
        except ImportError:
            self.skipTest("install requirements.txt and httpx for optional FastAPI tests")
        with TestClient(app) as client:
            for path in ("/", "/ride", "/cells", "/api/mapconfig", "/api/ride/profile/A",
                         "/api/demo/status"):
                self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.post("/api/ride/terrain", json={"coords": []}).status_code, 400)
        self.assertTrue(_in_bbox((48, 11.5)))
        self.assertTrue(_in_bbox((48, 11.5), [47, 11, 49, 12]))


if __name__ == "__main__":
    unittest.main()
