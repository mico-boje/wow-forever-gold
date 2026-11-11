"""NPC/object facts and map positions from captured Wowhead pages.

An NPC page carries the NPC itself (`$.extend(g_npcs[ID], {...})`: levels, hostility, elite,
zones) and, with userscript v5, its map positions (`g_mapperData`: per zone, a list of
[x, y] percentages on the zone map). That's enough to build a camp for a mob that's new in
Forever and has no spawn data in the vanilla database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .capture import capture_dir
from .jsdata import balanced, parse_js


@dataclass(frozen=True)
class EntityInfo:
    kind: str  # npc | object
    entity_id: int
    name: str
    level: tuple[int, int] | None = None
    react: tuple[int | None, int | None] | None = None  # (alliance, horde): -1/0/1
    elite: bool = False
    boss: bool = False
    zones: tuple[int, ...] = ()

    def friendly_to(self, faction: str) -> bool:
        return bool(self.react) and self.react[0 if faction == "alliance" else 1] == 1


@dataclass(frozen=True)
class MapPoint:
    zone_id: int | None
    zone_name: str | None
    x: float  # percent of the zone map, 0-100
    y: float


@dataclass
class Entity:
    info: EntityInfo | None
    points: list[MapPoint] = field(default_factory=list)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def entity_info(record: dict, kind: str, entity_id: int) -> EntityInfo | None:
    table = "g_npcs" if kind == "npc" else "g_objects"
    pattern = re.compile(rf"\$\.extend\(\s*{table}\[{entity_id}\]\s*,\s*\{{")
    for script in record.get("raw") or []:
        m = pattern.search(script)
        if not m:
            continue
        obj = parse_js(balanced(script, m.end() - 1) or "")
        if not isinstance(obj, dict):
            continue
        lo, hi = _num(obj.get("minlevel")), _num(obj.get("maxlevel"))
        react = obj.get("react")
        loc = obj.get("location") or []
        return EntityInfo(
            kind=kind,
            entity_id=entity_id,
            name=str(obj.get("name") or obj.get("displayName") or ""),
            level=(int(lo), int(hi or lo)) if lo else None,
            react=tuple(int(x) if _num(x) is not None else None for x in react)
            if isinstance(react, list) and len(react) == 2
            else None,
            elite=(_num(obj.get("classification")) or 0) >= 1,
            boss=bool(obj.get("boss")),
            zones=tuple(int(z) for z in loc if (_num(z) or 0) > 0) if isinstance(loc, list) else (),
        )
    return None


def _walk(obj, zone: int | None, out: list[MapPoint]) -> None:
    """Find every {"coords": [[x, y], ...]} in Wowhead's mapper data; the zone is the nearest
    numeric key above it."""
    if isinstance(obj, dict):
        coords = obj.get("coords")
        if isinstance(coords, list):
            name = obj.get("uiMapName") or obj.get("zoneName") or obj.get("name")
            for c in coords:
                if isinstance(c, list) and len(c) >= 2 and _num(c[0]) is not None:
                    out.append(
                        MapPoint(zone, str(name) if name else None, float(c[0]), float(c[1]))
                    )
        for k, v in obj.items():
            if k == "coords":
                continue
            key_zone = int(k) if str(k).isdigit() else None
            _walk(v, zone if zone is not None else key_zone, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, zone, out)


def map_points(record: dict) -> list[MapPoint]:
    data = record.get("mapper")
    if not data:
        for script in record.get("raw") or []:
            m = re.search(r"g_mapperData\s*=\s*\{", script)
            if m:
                data = parse_js(balanced(script, m.end() - 1) or "")
                break
    out: list[MapPoint] = []
    if isinstance(data, dict | list):
        _walk(data, None, out)
    return out


def load_entities(data_dir: Path) -> dict[tuple[str, int], Entity]:
    """Captured NPC/object pages: (kind, id) -> info and map positions."""
    out = {}
    for path in sorted(capture_dir(data_dir).glob("*.json")):
        kind, _, raw_id = path.stem.partition("-")
        if kind not in ("npc", "object") or not raw_id.isdigit():
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out[(kind, int(raw_id))] = Entity(
            entity_info(record, kind, int(raw_id)), map_points(record)
        )
    return out
