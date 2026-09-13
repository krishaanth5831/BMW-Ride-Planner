"""Shared, explicit BMW data selection for both launchers.

Only precomputed aggregates are loaded while serving. Never scan the entire
crowd lake in an HTTP request or blend unrelated riders into one profile.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from engine import demo, rider_profile
from engine.cache import lock_for, read_json

ROOT = Path(__file__).resolve().parents[1]
RIDERS = {"A": "exampleUserA", "B": "exampleUserB", "C": "exampleUserC"}


def dataset_root():
    configured = os.environ.get("BMW_DATASET")
    root = Path(configured or "~/Desktop/BMW/exd_download/datasetHackathon").expanduser()
    if configured and not root.is_dir():
        raise ValueError(f"BMW_DATASET does not exist: {root}")
    return root


def graph_path():
    return Path(os.environ.get("BMW_CELL_GRAPH", ROOT / "data" / "cell_graph.json")).expanduser()


def crowd_blob():
    with lock_for("crowd-blob"):
        return _crowd_blob()


@lru_cache(maxsize=1)
def _crowd_blob():
    path = graph_path()
    if not path.is_file():
        if os.environ.get("BMW_CELL_GRAPH"):
            raise ValueError(f"BMW_CELL_GRAPH does not exist: {path}")
        return demo.NEUTRAL_CELL_GRAPH
    blob = read_json(path)
    if not (isinstance(blob, dict) and blob.get("cells") and blob.get("edges")):
        raise ValueError(f"Invalid or empty BMW graph at {path}; rebuild it with "
                         "python -m precompute.build_cell_graph --dataset PATH")
    return blob


def profile(rider):
    rider = rider.upper()
    if rider not in RIDERS:
        raise ValueError("unknown rider; use A, B, or C")
    with lock_for(("profile", rider)):
        return _profile(rider)


@lru_cache(maxsize=3)
def _profile(rider):
    # A direct recordedTrips folder can be assigned explicitly to ONE rider.
    override = os.environ.get(f"BMW_RIDER_{rider}")
    folder = Path(override).expanduser() if override else dataset_root() / RIDERS[rider] / "recordedTrips"
    if not folder.is_dir():
        if override:
            raise ValueError(f"BMW_RIDER_{rider} does not exist: {folder}")
        return demo.sample_profile(rider)
    result = rider_profile.load_cached(str(folder))
    if not result.get("available"):
        raise ValueError(f"No valid BMW trips in {folder}; refusing to show sample data as telemetry")
    return {**result, "demo_mode": False, "profile_source": "BMW telemetry (all recorded trips)"}


def status():
    blob = crowd_blob()
    telemetry = bool(blob.get("cells"))
    return {
        "ok": True, "mode": "telemetry" if telemetry else "lightweight-demo",
        "crowd_source": "BMW telemetry aggregates" if telemetry else "neutral fallback (no crowd telemetry)",
        "crowd_cells": len(blob.get("cells", {})),
        "crowd_transitions": len(blob.get("edges", [])),
        "ingestion": blob.get("source", {}),
        "stats": blob.get("stats", {}),
        "road_source": "committed OpenStreetMap Bavaria extract",
        "riders": {r: {"source": profile(r)["profile_source"],
                        "trips": profile(r).get("trips", 0),
                        "sample": profile(r).get("demo_mode", False)} for r in RIDERS},
    }
