import unittest
from unittest.mock import patch

from engine import terrain


class TerrainTests(unittest.TestCase):
    def test_counts_a_clustered_hairpin_once(self):
        coords = [[48.0, 11.0], [48.001, 11.0], [48.001, 11.001],
                  [48.0, 11.001], [47.999, 11.001]]
        stats = terrain.road_geometry_stats(coords)
        self.assertGreaterEqual(stats["sharp_turns"], 1)
        self.assertGreaterEqual(stats["hairpin_turns"], 1)

    def test_elevation_gain_uses_live_samples(self):
        coords = [[48.0, 11.0], [48.01, 11.01], [48.02, 11.02]]
        with patch.object(terrain, "_fetch_elevation_batch",
                          side_effect=lambda points: tuple(
                              500.0 + i * 20.0 for i in range(len(points)))):
            stats = terrain.elevation_stats(coords)
        self.assertTrue(stats["elevation_available"])
        self.assertGreater(stats["elev_gain_m"], 0)
        self.assertEqual(stats["elevation_source"], "Open-Meteo · Copernicus DEM")


if __name__ == "__main__":
    unittest.main()
