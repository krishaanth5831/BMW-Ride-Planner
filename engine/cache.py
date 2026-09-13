"""Disposable JSON caches. Source data stays outside git and is never modified."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("BMW_CACHE_DIR", ROOT / "data" / "cache"))
_locks = {}
_guard = threading.Lock()
_retry_after = {}


def lock_for(key):
    with _guard:
        return _locks.setdefault(str(key), threading.RLock())


def signature(paths, version):
    records = []
    for filename in sorted(map(str, paths)):
        path = Path(filename).resolve()
        stat = path.stat()
        records.append((str(path), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps([version, records]).encode()).hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Atomically replace a derived file; readers never see half-written JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, separators=(",", ":"))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def derived_json(name, fingerprint, build):
    path = CACHE_DIR / name
    with lock_for(path):
        cached = read_json(path)
        if (isinstance(cached, dict) and cached.get("signature") == fingerprint
                and "value" in cached):
            return cached["value"]
        value = build()
        try:
            write_json(path, {"signature": fingerprint, "value": value})
        except OSError:
            pass  # A read-only install remains usable, just without disk caching.
        return value


def forecast_json(url, path, *, ttl, timeout, required):
    """Fresh-cache first, with one refresh per location and a failure cooldown.

    Stale data is marked explicitly and never relabeled as live weather.
    """
    from engine.net import https_context
    path = Path(path)
    with lock_for(path):
        now = time.time()
        data = read_json(path)
        valid = isinstance(data, dict) and required in data
        try:
            age = max(0.0, now - path.stat().st_mtime)
        except OSError:
            age = float("inf")
        if valid and age < ttl:
            return {**data, "source": "cache", "cache_age_s": round(age), "stale": False}
        if now >= _retry_after.get(str(path), 0):
            try:
                with urllib.request.urlopen(url, timeout=timeout,
                                            context=https_context()) as response:
                    fresh = json.load(response)
                if not isinstance(fresh, dict) or required not in fresh:
                    raise ValueError("incomplete forecast response")
                try:
                    write_json(path, fresh)
                except OSError:
                    pass
                _retry_after.pop(str(path), None)
                return {**fresh, "source": "live", "cache_age_s": 0, "stale": False}
            except (OSError, ValueError, TimeoutError):
                _retry_after[str(path)] = time.time() + 60
        if valid:
            return {**data, "source": "cache", "cache_age_s": round(age), "stale": True}
        return {"source": "unavailable"}
