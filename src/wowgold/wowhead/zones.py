"""Forever zones from captured Wowhead zone pages (`/forever/zone=ID`).

A zone page lists the NPCs and objects found in the zone. That's how wowgold learns what's in
a zone that doesn't exist in the vanilla database: its name, which hostile mobs live there (at
what level), and which herbs, ore veins and chests it has. Their own NPC/object pages (queued
on the Capture page) then add drop rates and map positions, which turn them into camps and
gathering loops.

Wowhead's listviews carry the details; this reads them tolerantly. NPC rows have id, name,
minlevel/maxlevel, react, classification. Object rows have id, name and a type: -3 for herbs
and -4 for mineral veins on Wowhead (other types are chests, quest objects and so on). Node
names that match the vanilla herbs and ores are recognised whatever the type says.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .capture import capture_dir
from .sources import _num, item_name_from_title, parse_raw_listviews

HERB_TYPE, VEIN_TYPE = -3, -4
# A listview's template is a name ("npc") in page scripts, but an object with an entityType
# in the captured g_listviews: 1 NPCs, 2 objects, 3 items, 5 quests.
ENTITY_TYPES = {1: "npc", 2: "object", 3: "item", 5: "quest"}
CRITTER_TYPE = 8  # NPC creature type
CHEST_WORDS = ("chest", "strongbox", "coffer", "crate", "cache", "footlocker")


@dataclass(frozen=True)
class ZoneNpc:
    id: int
    name: str
    level: tuple[int, int] | None
    react: tuple[int | None, int | None] | None
    elite: bool = False
    boss: bool = False
    critter: bool = False

    def hostile_to(self, faction: str) -> bool:
        """Attackable: not friendly to that faction (unknown counts as hostile)."""
        if not self.react:
            return True
        return self.react[0 if faction == "alliance" else 1] != 1


@dataclass(frozen=True)
class ZoneObject:
    id: int
    name: str
    kind: str  # herb | vein | chest | other
    skill: int | None = None  # gathering skill, when Wowhead lists it

    @property
    def profession(self) -> str | None:
        return {"herb": "herbalism", "vein": "mining"}.get(self.kind)


@dataclass
class ZonePage:
    zone_id: int
    name: str
    captured_at: str = ""
    npcs: list[ZoneNpc] = field(default_factory=list)
    objects: list[ZoneObject] = field(default_factory=list)

    @property
    def level(self) -> tuple[int, int] | None:
        """Typical level range of the zone's normal hostile mobs (10th-90th percentile)."""
        levels = sorted(
            lv
            for n in self.npcs
            if n.level and not (n.boss or n.elite or n.critter) and n.react != (1, 1)
            for lv in n.level
        )
        if not levels:
            return None
        return levels[len(levels) // 10], levels[(len(levels) * 9) // 10]


def _tabs(record: dict) -> list[tuple[str, str | None, list]]:
    tabs = [
        (tab_id, lv.get("template"), lv.get("data") or [])
        for tab_id, lv in (record.get("listviews") or {}).items()
        if isinstance(lv, dict)
    ]
    seen = {t[0] for t in tabs}
    for script in record.get("raw") or []:
        for tab_id, template, rows in parse_raw_listviews(script):
            if tab_id not in seen:
                seen.add(tab_id)
                tabs.append((tab_id, template, rows))
    return tabs


def _level(row: dict) -> tuple[int, int] | None:
    lo, hi = _num(row.get("minlevel")), _num(row.get("maxlevel"))
    if not lo and not hi:
        return None
    lo = lo or hi
    return int(lo), int(hi or lo)


def _object_kind(row: dict, tab_id: str, herbs: set[str], ores: set[str]) -> str:
    name = str(row.get("name") or "")
    kind = _num(row.get("type"))
    tab = tab_id.lower()
    if kind == HERB_TYPE or name in herbs or "herb" in tab:
        return "herb"
    if kind == VEIN_TYPE or name in ores or "min" in tab or "vein" in tab:
        return "vein"
    if any(w in name.lower() for w in CHEST_WORDS):
        return "chest"
    return "other"


def zone_from_capture(
    record: dict, zone_id: int, herbs: set[str] = frozenset(), ores: set[str] = frozenset()
) -> ZonePage:
    """`herbs`/`ores`: known node names (from the vanilla data) to recognise nodes by name."""
    title = str(record.get("title") or "")
    page = ZonePage(
        zone_id,
        item_name_from_title(title) or f"zone {zone_id}",
        str(record.get("captured_at") or ""),
    )
    npcs: dict[int, ZoneNpc] = {}
    objects: dict[int, ZoneObject] = {}
    for tab_id, template, rows in _tabs(record):
        if not isinstance(rows, list):
            continue
        if isinstance(template, dict):
            tmpl = ENTITY_TYPES.get(int(_num(template.get("entityType")) or 0), "")
        else:
            tmpl = str(template or "").lower()
        for row in rows:
            if not isinstance(row, dict) or _num(row.get("id")) is None:
                continue
            rid, name = int(row["id"]), str(row.get("name") or "")
            if tmpl == "npc" or (not tmpl and tab_id.lower().startswith("npc")):
                react = row.get("react")
                npcs.setdefault(
                    rid,
                    ZoneNpc(
                        rid,
                        name,
                        _level(row),
                        tuple(int(x) if _num(x) is not None else None for x in react)
                        if isinstance(react, list) and len(react) == 2
                        else None,
                        elite=(_num(row.get("classification")) or 0) in (1, 2),
                        boss=bool(row.get("boss")) or (_num(row.get("classification")) or 0) == 3,
                        critter=_num(row.get("type")) == CRITTER_TYPE,
                    ),
                )
            elif tmpl == "object" or (not tmpl and tab_id.lower().startswith("object")):
                skill = _num(row.get("skill"))
                objects.setdefault(
                    rid,
                    ZoneObject(
                        rid,
                        name,
                        _object_kind(row, tab_id, herbs, ores),
                        int(skill) if skill else None,
                    ),
                )
    page.npcs = sorted(npcs.values(), key=lambda n: (n.level or (0, 0), n.name))
    page.objects = sorted(objects.values(), key=lambda o: (o.kind, o.name))
    return page


def load_zones(
    data_dir: Path, herbs: set[str] = frozenset(), ores: set[str] = frozenset()
) -> dict[int, ZonePage]:
    out = {}
    for path in sorted(capture_dir(data_dir).glob("zone-*.json")):
        m = re.fullmatch(r"zone-(\d+)", path.stem)
        if not m:
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out[int(m[1])] = zone_from_capture(record, int(m[1]), herbs, ores)
    return out


# -- what to capture next ---------------------------------------------------------------------

WOWHEAD = "https://www.wowhead.com/forever"


@dataclass(frozen=True)
class ZoneRow:
    key: str  # capture key: zone-1 / npc-2 / object-3
    url: str
    name: str
    note: str  # level, elite, herb, ...


@dataclass
class ZoneQueue:
    zone_id: int
    name: str
    level: tuple[int, int] | None
    captured: bool  # the zone page itself
    rows: list[ZoneRow] = field(default_factory=list)
    hidden: int = 0  # rows past the limit


def zone_queue(
    zones: dict[int, ZonePage],
    known_ids: set[int],
    faction: str,
    level: int | None = None,
    limit: int = 100,
) -> list[ZoneQueue]:
    """Per Forever zone: its page (if not captured yet), then its herbs, veins and chests, then
    its hostile mobs (closest to `level` first, elites last), for their own pages."""
    out = []
    for zid in sorted(known_ids | set(zones)):
        z = zones.get(zid)
        q = ZoneQueue(zid, z.name if z else f"zone {zid}", z.level if z else None, z is not None)
        q.rows.append(ZoneRow(f"zone-{zid}", f"{WOWHEAD}/zone={zid}", q.name, "zone page"))
        if z:
            order = {"herb": 0, "vein": 1, "chest": 2}
            for o in sorted(z.objects, key=lambda o: (order.get(o.kind, 9), o.name)):
                if o.kind in order:
                    q.rows.append(
                        ZoneRow(
                            f"object-{o.id}",
                            f"{WOWHEAD}/object={o.id}",
                            o.name,
                            {"herb": "herb", "vein": "ore vein", "chest": "chest"}[o.kind]
                            + (f", skill {o.skill}" if o.skill else ""),
                        )
                    )
            mobs = [n for n in z.npcs if n.hostile_to(faction) and not (n.boss or n.critter)]

            def closeness(n: ZoneNpc) -> tuple:
                if not n.level:
                    return (n.elite, 1, 99)
                mid = sum(n.level) / 2
                return (n.elite, 0, abs(mid - level) if level else mid)

            mobs.sort(key=closeness)
            for n in mobs:
                lvl = (
                    f"lvl {n.level[0]}" + (f"–{n.level[1]}" if n.level[1] != n.level[0] else "")
                    if n.level
                    else "lvl ?"
                )
                q.rows.append(
                    ZoneRow(
                        f"npc-{n.id}",
                        f"{WOWHEAD}/npc={n.id}",
                        n.name,
                        lvl + (", elite" if n.elite else ""),
                    )
                )
        q.hidden = max(0, len(q.rows) - limit)
        q.rows = q.rows[:limit]
        out.append(q)
    return out
