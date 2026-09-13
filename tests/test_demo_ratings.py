import math
import unittest

from engine.demo import STYLE_RATINGS, sample_profile
from engine.demo_server import runtime, suggest_payload


class DemoRatingTests(unittest.TestCase):
    def test_exactly_45_bounded_ratings(self):
        ratings = [score
                   for rider in STYLE_RATINGS.values()
                   for score in rider["ratings"].values()]
        self.assertEqual(len(ratings), 45)
        self.assertTrue(all(1 <= score <= 10 for score in ratings))

    def test_archetype_signals_match_descriptions(self):
        a = STYLE_RATINGS["A"]["ratings"]
        b = STYLE_RATINGS["B"]["ratings"]
        c = STYLE_RATINGS["C"]["ratings"]
        self.assertGreater(a["Long-distance endurance"], b["Long-distance endurance"])
        self.assertGreater(b["Lean commitment"], c["Lean commitment"])
        self.assertGreater(c["Braking intensity"], b["Braking intensity"])
        self.assertGreater(c["Throttle intensity"], a["Throttle intensity"])

    def test_profile_exposes_rating_provenance(self):
        profile = sample_profile("A")
        self.assertEqual(profile["rider_style"], "The Tourer")
        self.assertEqual(profile["rating_scale"], {"min": 1, "max": 10})
        self.assertIn("not telemetry", profile["profile_source"])

    def test_every_mapped_poi_has_three_rankings(self):
        pois = runtime()[2].pois
        self.assertEqual(len(pois), 450)
        for poi in pois:
            self.assertEqual(set(poi["rider_fit"]), {"A", "B", "C"})
            self.assertTrue(all(1 <= score <= 10
                                for score in poi["rider_fit"].values()))
        for rider in ("A", "B", "C"):
            ranks = sorted(poi["rider_rank"][rider]["rank"] for poi in pois)
            self.assertEqual(ranks, list(range(1, len(pois) + 1)))

    def test_poi_style_ownership_uses_top_thirty_percent(self):
        pois = runtime()[2].pois
        cutoff = math.ceil(len(pois) * .30)
        owners = {rider: [p for p in pois if p["rider_style_owner"] == rider]
                  for rider in ("A", "B", "C")}
        self.assertEqual(sum(map(len, owners.values())), len(pois))
        self.assertTrue(owners["A"] and owners["B"] and owners["C"])
        self.assertLessEqual(len(owners["B"]), cutoff)
        self.assertLessEqual(len(owners["C"]), cutoff)
        self.assertTrue(all(p["style_features"]["curvature_rank"] <= cutoff
                            for p in owners["B"]))
        self.assertTrue(all(p["style_features"]["speed_rank"] <= cutoff
                            for p in owners["C"]))

    def test_profiles_produce_distinct_destinations(self):
        same_request = {"minutes": 150,
                        "origin": [48.137154, 11.576124],
                        "weather": 0,
                        "terrain": False,
                        "seed": 20260913}
        planned = [suggest_payload({**same_request, "rider": rider})
                   for rider in ("A", "B", "C")]
        route_shapes = [tuple(map(tuple, result["routes"][0]["coords"]))
                        for result in planned]
        self.assertGreaterEqual(len(set(route_shapes)), 2)
        for rider, result in zip(("A", "B", "C"), planned):
            route = result["routes"][0]
            self.assertEqual(route["personalization"]["rider"], rider)
            self.assertTrue(all("rider_rating" in stop and "rider_rank" in stop
                                for stop in route.get("stops", [])))
            self.assertEqual(route["spot_search"]["mode"],
                             "personalized scenic orienteering")
            self.assertGreater(route["spot_search"]["cool_spot_count"], 0)
            self.assertGreater(route["spot_search"]["candidate_pois_evaluated"], 0)
            if result["kind"] == "loop":
                self.assertLessEqual(route["overlap"], .10)
            else:
                self.assertLessEqual(route["kpis"]["retrace_km"], 8.0)
                self.assertLessEqual(route["kpis"]["retrace_share"], .10)

    def test_seeded_variations_are_reproducible_and_diverse(self):
        request = {"minutes": 150,
                   "origin": [48.137154, 11.576124],
                   "weather": 0,
                   "terrain": False,
                   "rider": "A"}
        first = suggest_payload({**request, "seed": 17})["routes"][0]
        repeated = suggest_payload({**request, "seed": 17})["routes"][0]
        alternate = suggest_payload({**request, "seed": 1})["routes"][0]
        self.assertEqual(first["coords"], repeated["coords"])
        self.assertEqual(first["spot_search"]["variation_seed"], 17)
        self.assertNotEqual(first["coords"], alternate["coords"])


if __name__ == "__main__":
    unittest.main()
