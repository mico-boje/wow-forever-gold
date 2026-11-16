"""Farming routes: the points of a camp or gathering loop, ordered into a short circuit.

A route is either in **world** coordinates (vanilla spawns: yards on continent 0 or 1, as the
database stores them; the addon converts them to map coordinates in game) or in **map**
coordinates (Wowhead's positions for Forever zones: percent of the zone map, with the game's
uiMapID). The WowGoldLogger addon turns routes into RXPGuides `.loop` guides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MAX_POINTS = 60
MERGE_WORLD_YD = 30.0  # spawns closer than this are one stop
MERGE_MAP_PCT = 1.0


@dataclass
class Route:
    kind: str  # world | map
    map_id: int  # continent (world) or uiMapID (map)
    points: list[tuple[float, float]] = field(default_factory=list)
    range: int = 25  # yards: "arrived" for RXP's arrow
    mobs: list[str] = field(default_factory=list)  # what to kill, most valuable first

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "map": self.map_id,
            "range": self.range,
            "mobs": self.mobs,
            "points": [[round(x, 1), round(y, 1)] for x, y in self.points],
        }


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _cluster(points: list[tuple[float, float]], cell: float) -> list[tuple[float, float]]:
    """Centroid of the points in each `cell`-sized square."""
    cells: dict[tuple[int, int], list] = {}
    for x, y in points:
        cells.setdefault((int(x // cell), int(y // cell)), []).append((x, y))
    return [
        (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps)) for ps in cells.values()
    ]


def order_loop(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A short closed tour: nearest neighbour from the westmost point, then 2-opt."""
    if len(points) <= 3:
        return list(points)
    left = list(points)
    tour = [left.pop(min(range(len(left)), key=lambda i: (left[i][0], left[i][1])))]
    while left:
        i = min(range(len(left)), key=lambda i: _dist(tour[-1], left[i]))
        tour.append(left.pop(i))
    n = len(tour)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 2, n if i else n - 1):
                a, b, c, d = tour[i], tour[i + 1], tour[j], tour[(j + 1) % n]
                if _dist(a, c) + _dist(b, d) < _dist(a, b) + _dist(c, d) - 1e-9:
                    tour[i + 1 : j + 1] = reversed(tour[i + 1 : j + 1])
                    improved = True
    return tour


MAX_MOBS = 6


def make_route(
    kind: str, map_id: int, points, *, mobs=(), max_points: int = MAX_POINTS
) -> Route | None:
    pts = [(float(x), float(y)) for x, y in points]
    if not pts:
        return None
    cell = MERGE_WORLD_YD if kind == "world" else MERGE_MAP_PCT
    pts = _cluster(pts, cell)
    while len(pts) > max_points:
        cell *= 1.5
        pts = _cluster(pts, cell)
    names = list(dict.fromkeys(n for n in mobs if n))[:MAX_MOBS]
    return Route(kind, int(map_id), order_loop(pts), mobs=names)
