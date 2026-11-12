"""Guess the zone of a world position.

The world DB has no zone per spawn (that lives in the client's map files). It does have
two things tied to both a zone and a position:

- node pools, whose descriptions name the zone ("Dreamfoil - Azshara - Group 1 - Master");
- quest objectives: creatures you're asked to kill, with the quest's zone.

Each becomes a labelled reference point; a position takes the majority zone of its nearest
reference points. Good enough to say where a camp is, not to draw zone borders.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass

from .vanilla import CONTINENTS

CELL = 250.0  # yards per grid cell used for lookups
MAX_DIST = 900.0  # beyond this, a reference point says nothing
K = 9

# Spellings used in pool descriptions that differ from the official zone name.
ALIASES = {"Barrens": "The Barrens", "Hinterlands": "The Hinterlands"}


@dataclass
class ZoneIndex:
    grid: dict[tuple[int, int, int], list[tuple[float, float, str]]]

    def zone_at(self, map_id: int, x: float, y: float) -> str | None:
        cx, cy = int(x // CELL), int(y // CELL)
        reach = math.ceil(MAX_DIST / CELL)
        near = []
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for px, py, zone in self.grid.get((map_id, cx + dx, cy + dy), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= MAX_DIST:
                        near.append((d, zone))
        if not near:
            return None
        near.sort()
        votes: Counter[str] = Counter()
        for d, zone in near[:K]:
            votes[zone] += 1 / (1 + d / 100)
        return votes.most_common(1)[0][0]


def _zone_names(conn: sqlite3.Connection) -> dict[int, str]:
    marks = ",".join("?" * len(CONTINENTS))
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT entry, name FROM area_template WHERE zone_id = 0 AND map_id IN ({marks})",
            CONTINENTS,
        )
    }


def _match_zone(text: str, names: list[str]) -> str | None:
    """Longest zone name contained in a pool description."""
    best = None
    for name in names:
        bare = name.removeprefix("The ")
        if (name in text or bare in text) and (best is None or len(name) > len(best)):
            best = name
    if best is None:
        for alias, name in ALIASES.items():
            if alias in text:
                return name
    return best


def build_zone_index(conn: sqlite3.Connection) -> ZoneIndex:
    zone_ids = _zone_names(conn)
    names = sorted(set(zone_ids.values()))
    grid: dict[tuple[int, int, int], list[tuple[float, float, str]]] = {}

    def add(map_id: int, x: float, y: float, zone: str) -> None:
        grid.setdefault((map_id, int(x // CELL), int(y // CELL)), []).append((x, y, zone))

    # Pool descriptions; a group pool often only names the node, so fall back to its mother.
    desc = {r[0]: r[1] or "" for r in conn.execute("SELECT entry, description FROM pool_template")}
    mother = {r[0]: r[1] for r in conn.execute("SELECT pool_id, mother_pool FROM pool_pool")}
    pool_zone: dict[int, str | None] = {}
    for pool in desc:
        zone = _match_zone(desc[pool], names)
        if zone is None and pool in mother:
            zone = _match_zone(desc.get(mother[pool], ""), names)
        pool_zone[pool] = zone
    marks = ",".join("?" * len(CONTINENTS))
    for map_id, x, y, pool in conn.execute(
        f"""SELECT g.map, g.position_x, g.position_y, p.pool_entry
            FROM gameobject g JOIN pool_gameobject p ON p.guid = g.guid
            WHERE g.map IN ({marks})""",
        CONTINENTS,
    ):
        if zone := pool_zone.get(pool):
            add(map_id, x, y, zone)

    # Kill objectives: the target's spawns are in the quest's zone (usually).
    targets: dict[int, Counter[str]] = {}
    for row in conn.execute(
        "SELECT ZoneOrSort, ReqCreatureOrGOId1, ReqCreatureOrGOId2, ReqCreatureOrGOId3, "
        "ReqCreatureOrGOId4 FROM quest_template WHERE ZoneOrSort > 0"
    ):
        zone = zone_ids.get(row[0])
        if not zone:
            continue
        for target in row[1:]:
            if target and target > 0:
                targets.setdefault(target, Counter())[zone] += 1
    for map_id, x, y, entry in conn.execute(
        f"SELECT map, position_x, position_y, id FROM creature WHERE map IN ({marks})",
        CONTINENTS,
    ):
        if (votes := targets.get(entry)) and len(votes) == 1:
            add(map_id, x, y, next(iter(votes)))
    return ZoneIndex(grid)
