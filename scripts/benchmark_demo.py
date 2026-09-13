"""Offline, repeatable timings; excludes external weather/elevation services."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--minutes", type=float, default=150)
    args = parser.parse_args()
    from engine import demo_server, ride_data
    start = time.perf_counter()
    _, graph, pois = demo_server.runtime()
    print(json.dumps({"step": "startup", "seconds": round(time.perf_counter() - start, 3),
                      "roads": len(graph.segments), "pois": len(pois.pois),
                      "crowd_cells": len(ride_data.crowd_blob().get("cells", {}))}), flush=True)
    for rider in "ABC":
        start = time.perf_counter()
        result = demo_server.suggest_payload({"rider": rider, "minutes": args.minutes,
                                              "seed": args.seed, "terrain": False})
        print(json.dumps({"step": "route", "rider": rider,
                          "seconds": round(time.perf_counter() - start, 3),
                          "routes": len(result["routes"]),
                          "sha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}), flush=True)


if __name__ == "__main__":
    main()
