"""Small exact spatial index; no optional scientific packages required.

Bounding boxes only prune impossible candidates. Final distances and tie
breaking are identical to the previous full scan, not approximate neighbours.
"""

from __future__ import annotations

import math


class PointIndex:
    def __init__(self, points):
        # Keep original order for exactly tied distances.
        items = [(lat, lon, order, key)
                 for order, (key, lat, lon) in enumerate(points)]
        self.root = self._build(items)

    @classmethod
    def _build(cls, items):
        if not items:
            return None
        bounds = (min(p[0] for p in items), min(p[1] for p in items),
                  max(p[0] for p in items), max(p[1] for p in items))
        if len(items) <= 24:
            return bounds, items, None, None
        axis = 0 if bounds[2] - bounds[0] >= bounds[3] - bounds[1] else 1
        items.sort(key=lambda p: p[axis])
        mid = len(items) // 2
        return bounds, None, cls._build(items[:mid]), cls._build(items[mid:])

    def nearest(self, lat, lon):
        scale = math.cos(math.radians(lat))
        best_d, best_order, best_key = float("inf"), float("inf"), None

        def bound(node):
            south, west, north, east = node[0]
            dy = max(south - lat, 0.0, lat - north)
            dx = max(west - lon, 0.0, lon - east)
            return dy ** 2 + (dx * scale) ** 2

        def visit(node):
            nonlocal best_d, best_order, best_key
            if node is None or bound(node) > best_d:
                return
            _, items, left, right = node
            if items is not None:
                for nlat, nlon, order, key in items:
                    distance = (nlat - lat) ** 2 + ((nlon - lon) * scale) ** 2
                    if (distance, order) < (best_d, best_order):
                        best_d, best_order, best_key = distance, order, key
            else:
                if bound(left) > bound(right):
                    left, right = right, left
                visit(left)
                visit(right)

        visit(self.root)
        return best_key

    def in_box(self, south, west, north, east):
        out = []

        def visit(node):
            if node is None:
                return
            (s, w, n, e), items, left, right = node
            if n < south or s > north or e < west or w > east:
                return
            if items is not None:
                out.extend(key for lat, lon, _, key in items
                           if south <= lat <= north and west <= lon <= east)
            else:
                visit(left)
                visit(right)

        visit(self.root)
        return out
