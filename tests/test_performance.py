"""Regression tests: speedups must not change route selection or hide data."""

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from engine import cache, ride_data, rider_profile, terrain
from engine.spatial import PointIndex
from precompute.build_cell_graph import collect_paths, main as build_crowd


def trip_file(path, trip="one"):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["trip_id", "timestampinmillis", "positionmapmatchedlatitude",
              "positionmapmatchedlongitude", "sensorsbankingangle",
              "ridingenginespeed", "ridinggear"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i in range(12):
            writer.writerow(dict(zip(fields, [trip, 1700000000000 + i * 3000,
                                               48.0 + i * .0008, 11.5,
                                               12, 3000, 4])))


class SpatialTests(unittest.TestCase):
    def test_exact_nearest_matches_full_scan_including_ties(self):
        rng = random.Random(11)
        points = [(i, rng.uniform(-85, 85), rng.uniform(-175, 175))
                  for i in range(1800)]
        points += [(1800, points[0][1], points[0][2])]
        index = PointIndex(points)
        for lat, lon in [(points[0][1], points[0][2]), (90, 10), (-90, 3)] + [
                (rng.uniform(-89, 89), rng.uniform(-179, 179)) for _ in range(100)]:
            scale = math.cos(math.radians(lat))
            expected = min(points, key=lambda p: (p[1] - lat) ** 2
                           + ((p[2] - lon) * scale) ** 2)[0]
            self.assertEqual(index.nearest(lat, lon), expected)
        self.assertIsNone(PointIndex([]).nearest(48, 11))

    def test_box_is_inclusive(self):
        index = PointIndex([(1, 0, 0), (2, 1, 1), (3, 2, 2)])
        self.assertEqual(set(index.in_box(0, 0, 1, 1)), {1, 2})
        self.assertEqual(index.in_box(3, 3, 4, 4), [])


class CacheAndDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_patch = patch.object(cache, "CACHE_DIR", self.root / "cache")
        self.cache_patch.start()
        ride_data._profile.cache_clear()
        ride_data._crowd_blob.cache_clear()

    def tearDown(self):
        ride_data._profile.cache_clear()
        ride_data._crowd_blob.cache_clear()
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_derived_cache_invalidates(self):
        self.assertEqual(cache.derived_json("test.json", "a", lambda: [1]), [1])
        self.assertEqual(cache.derived_json("test.json", "a", lambda: [2]), [1])
        self.assertEqual(cache.derived_json("test.json", "b", lambda: [2]), [2])
        (self.root / "cache" / "test.json").write_text("broken")
        self.assertEqual(cache.derived_json("test.json", "b", lambda: [3]), [3])

    def test_all_rider_files_not_only_sixty_and_cache_refreshes(self):
        folder = self.root / "recordedTrips"
        for i in range(61):
            trip_file(folder / f"{i:03}.csv", str(i))
        profile = rider_profile.load_cached(str(folder))
        self.assertEqual(profile["trips"], 61)
        with patch.object(rider_profile, "build", side_effect=AssertionError("cache missed")):
            self.assertEqual(rider_profile.load_cached(str(folder)), profile)
        trip_file(folder / "062.csv", "62")
        self.assertEqual(rider_profile.load_cached(str(folder))["trips"], 62)
        self.assertEqual(rider_profile.build(str(folder), limit=2)["trips"], 2)

    def test_direct_folder_is_assigned_to_one_rider(self):
        folder = self.root / "recordedTrips"
        trip_file(folder / "one.csv")
        with patch.dict(os.environ, {"BMW_DATASET": str(self.root), "BMW_RIDER_A": str(folder)}):
            self.assertFalse(ride_data.profile("A")["demo_mode"])
            self.assertEqual(ride_data.profile("A")["trips"], 1)
            self.assertTrue(ride_data.profile("B")["demo_mode"])

    def test_configured_but_empty_rider_is_not_replaced_with_fake_data(self):
        with patch.dict(os.environ, {"BMW_RIDER_A": str(self.root)}):
            with self.assertRaisesRegex(ValueError, "No valid BMW trips"):
                ride_data.profile("A")

    def test_complete_lake_including_ff_shards_and_provenance(self):
        for shard in ("00", "60", "ff"):
            trip_file(self.root / "dataset" / "anonymizedDataLake" /
                      "trips-samples-1" / shard / f"{shard}.csv", shard)
        self.assertEqual(len(collect_paths([str(self.root / "dataset")] * 2)), 3)
        self.assertEqual(build_crowd(["build", "--dataset", str(self.root / "dataset"),
                                     "--out", str(self.root / "out")]), 0)
        with patch.dict(os.environ, {"BMW_CELL_GRAPH": str(self.root / "out" / "cell_graph.json")}):
            blob = ride_data.crowd_blob()
            self.assertTrue(blob["cells"])
            self.assertEqual(blob["source"]["files_processed"], 3)
            self.assertIsNone(blob["source"]["file_limit"])

    def test_failed_build_preserves_previous_graph(self):
        output = self.root / "cell_graph.json"
        output.write_text('{"previous":"keep me"}')
        with self.assertRaises(SystemExit):
            build_crowd(["build", "--dataset", str(self.root / "missing"),
                         "--out", str(self.root)])
        self.assertEqual(output.read_text(), '{"previous":"keep me"}')
        with patch.dict(os.environ, {"BMW_CELL_GRAPH": str(output)}):
            with self.assertRaisesRegex(ValueError, "Invalid or empty"):
                ride_data.crowd_blob()

    def test_fresh_weather_skips_network_and_stale_cache_has_cooldown(self):
        target = self.root / "forecast.json"
        cache.write_json(target, {"hourly": {"time": []}})
        with patch("urllib.request.urlopen", side_effect=OSError("offline")) as network:
            result = cache.forecast_json("https://example.test", target, ttl=900,
                                         timeout=1, required="hourly")
            self.assertFalse(result["stale"])
            network.assert_not_called()
            os.utime(target, (0, 0))
            first = cache.forecast_json("https://example.test", target, ttl=900,
                                        timeout=1, required="hourly")
            second = cache.forecast_json("https://example.test", target, ttl=900,
                                         timeout=1, required="hourly")
            self.assertTrue(first["stale"])
            self.assertEqual(second["source"], "cache")
            self.assertEqual(network.call_count, 1)

    def test_cold_weather_is_fetched_and_then_cached(self):
        target = self.root / "fresh.json"
        with patch("urllib.request.urlopen", return_value=io.StringIO('{"hourly":{"time":[]}}')) as network:
            self.assertEqual(cache.forecast_json("https://example.test", target, ttl=900,
                             timeout=1, required="hourly")["source"], "live")
            self.assertEqual(cache.forecast_json("https://example.test", target, ttl=900,
                             timeout=1, required="hourly")["source"], "cache")
            self.assertEqual(network.call_count, 1)


class RoutingRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engine import demo, demo_server
        cls.profiles = patch.object(ride_data, "profile", side_effect=demo.sample_profile)
        cls.crowd = patch.object(ride_data, "crowd_blob", return_value=demo.NEUTRAL_CELL_GRAPH)
        cls.profiles.start()
        cls.crowd.start()
        demo_server._runtime.cache_clear()

    @classmethod
    def tearDownClass(cls):
        from engine import demo_server
        demo_server._runtime.cache_clear()
        cls.profiles.stop()
        cls.crowd.stop()

    def test_seeded_responses_match_original_main_exactly(self):
        from engine.demo_server import suggest_payload
        hashes = {
            "A": "ccfe5215f4220c59e05f07f2aff1c241cb8d94c19af9c4100b7163622cc2cf9e",
            "B": "e97344c473d4c6c4ccc40de0a4bfa68ffe5efb6d10c20d23ff0ecc4d6e9d0c1c",
            "C": "de72111fb452f425c44f0a01e10e7176b73ecee8a88a75c80c8284fc4a5c6a24",
        }
        # Golden responses captured from main 6a8cda4, including coordinates,
        # scores, POI ordering, overlap rejection and all fallback routes.
        for rider, expected in hashes.items():
            with self.subTest(rider=rider):
                result = suggest_payload({"rider": rider, "minutes": 150,
                                          "seed": 42, "terrain": False})
                self.assertEqual(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(), expected)

    def test_route_visits_match_original_full_scan(self):
        from engine.demo_server import runtime
        from engine.scenic import haversine_m, VISIT_M
        index = runtime()[2]
        coords = [[p["target_lat"], p["target_lon"]] for p in index.pois[::10]]
        points = coords[::3] or coords
        reference = {p["name"] + str(p["node"]): min(
            haversine_m((p["target_lat"], p["target_lon"]), tuple(c)) for c in points)
            for p in index.pois}
        expected = sorted((p["name"], round(reference[p["name"] + str(p["node"])]))
                          for p in index.pois if reference[p["name"] + str(p["node"])] <= VISIT_M)
        self.assertEqual(sorted((p["name"], p["distance_m"]) for p in index.visited_by(coords)), expected)

    def test_terrain_validation_before_network(self):
        with patch.object(terrain, "elevation_stats") as fetch:
            for coords in ([], [[0, 0]], [[float("nan"), 10], [48, 11]], [[91, 10], [48, 11]]):
                with self.assertRaises(ValueError):
                    terrain.payload({"coords": coords})
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
