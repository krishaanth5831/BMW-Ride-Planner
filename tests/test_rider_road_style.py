import unittest

from engine.cell_osm_router import RoadCost


class _Graph:
    @staticmethod
    def seconds(_segment):
        return 60.0


class RiderRoadStyleTests(unittest.TestCase):
    def test_tourer_is_slower_on_the_same_road(self):
        segment = {"highway": "secondary", "curvature_geo": 0}
        tourer = RoadCost(_Graph(), None, rider_style="A", rider_speed_kmh=54)
        all_rounder = RoadCost(_Graph(), None, rider_style="C", rider_speed_kmh=66)
        self.assertGreater(tourer.seconds(segment), all_rounder.seconds(segment))

    def test_tourer_strongly_penalizes_corner_rider_roads(self):
        sharp = {"highway": "secondary", "curvature_geo": 400}
        tourer = RoadCost(_Graph(), None, rider_style="A", rider_speed_kmh=54)
        corner_rider = RoadCost(_Graph(), None, rider_style="B", rider_speed_kmh=64)
        self.assertGreaterEqual(tourer.rider_style_factor(sharp), 4.0)
        self.assertEqual(corner_rider.rider_style_factor(sharp), 1.0)


if __name__ == "__main__":
    unittest.main()
