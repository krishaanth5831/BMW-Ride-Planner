"""Morton (base-4 quadkey) encoder, ported verbatim from BMW's own tripViewer.

Reference: tripViewer/tripViewer/app.js:225 (lonLatToTileXY) and the comment
block at app.js:63. The scheme is an EQUIRECTANGULAR square lat/lon grid --
NOT Web Mercator:

    xf = (lon + 180) / 360
    yf = (lat +  90) / 360        <- note: also divided by 360, not 180

and each level's digit is 2*latBit + lonBit, most significant first.

Rolling our own tiling would silently misplace every segment, so this is
verified against the dataset's own `morton_code` column by
`verify_against_dataset()` below. That check is Gate 0: run it before any
aggregation, because a wrong encoder invalidates the whole pass.
"""

from __future__ import annotations

# The dataset ships 32 base-4 digits per row.
MAX_LEVEL = 32

# Cell edge length in degrees is 360 / 2**level; at 48 deg N the ground width is
# that times 111_320 * cos(lat). Useful reference points:
#   level 14 -> ~1.5 km      (coarse rollups, coverage, temporal buckets)
#   level 18 -> ~92 m        (node identity for the crowd graph)
LEVEL_NODE = 18
LEVEL_COARSE = 14


def lonlat_to_tile_xy(lat: float, lon: float, level: int) -> tuple[int, int]:
    """Tile indices at `level`. y grows northward, matching app.js:225."""
    n = 1 << level
    x = int(((lon + 180.0) / 360.0) * n)
    y = int(((lat + 90.0) / 360.0) * n)
    clamp = lambda v: min(n - 1, max(0, v))  # noqa: E731
    return clamp(x), clamp(y)


def morton_code(lat: float, lon: float, level: int = MAX_LEVEL) -> str:
    """Base-4 quadkey string of `level` digits, interleaving lat and lon bits."""
    x, y = lonlat_to_tile_xy(lat, lon, level)
    digits = []
    for shift in range(level - 1, -1, -1):
        lon_bit = (x >> shift) & 1
        lat_bit = (y >> shift) & 1
        digits.append(str(2 * lat_bit + lon_bit))
    return "".join(digits)


def tile_bounds(x: int, y: int, level: int) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a cell. y+1 is the north edge."""
    n = 1 << level
    return (
        (y / n) * 360.0 - 90.0,
        (x / n) * 360.0 - 180.0,
        ((y + 1) / n) * 360.0 - 90.0,
        ((x + 1) / n) * 360.0 - 180.0,
    )


def cell_center(code: str) -> tuple[float, float]:
    """Centre (lat, lon) of the cell a morton prefix names."""
    x = y = 0
    for digit in code:
        d = int(digit)
        x = (x << 1) | (d & 1)
        y = (y << 1) | (d >> 1)
    south, west, north, east = tile_bounds(x, y, len(code))
    return (south + north) / 2.0, (west + east) / 2.0


def cell_size_m(level: int, lat: float = 48.14) -> float:
    """Approximate E-W ground width of a cell in metres."""
    import math

    return (360.0 / (1 << level)) * 111_320.0 * math.cos(math.radians(lat))


def verify_against_dataset(csv_path: str, level: int = LEVEL_NODE, rows: int = 1000) -> None:
    """Gate 0. Assert our encoder reproduces the dataset's own `morton_code`.

    Every raw row carries the authoritative code, so this is exact ground truth
    and needs no map viewer. Raises AssertionError with a sample of mismatches.
    """
    import csv as _csv

    checked = 0
    bad: list[str] = []
    with open(csv_path, newline="") as fh:
        for row in _csv.DictReader(fh):
            truth = (row.get("morton_code") or "").strip()
            try:
                lat = float(row["positionmapmatchedlatitude"])
                lon = float(row["positionmapmatchedlongitude"])
            except (TypeError, ValueError):
                continue
            # Rows with no fix carry 0/0 and no meaningful code.
            if not truth or (lat == 0.0 and lon == 0.0):
                continue
            ours = morton_code(lat, lon, level)
            if ours != truth[:level]:
                bad.append(f"{lat},{lon}: ours={ours} truth={truth[:level]}")
            checked += 1
            if checked >= rows:
                break

    assert checked > 0, f"no usable rows in {csv_path}"
    assert not bad, (
        f"morton encoder disagrees with the dataset on {len(bad)}/{checked} rows. "
        f"First few: {bad[:3]}"
    )
    print(f"Gate 0 OK: {checked} rows, encoder matches dataset morton_code at level {level}")


if __name__ == "__main__":
    import sys

    verify_against_dataset(*sys.argv[1:2] or ["."])
