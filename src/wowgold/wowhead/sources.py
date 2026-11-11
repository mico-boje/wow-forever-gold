"""Turn captured Wowhead item pages into item sources (who drops it, what it's mined from...).

Wowhead renders those tabs with `Listview` tables. The userscript sends the page's live
`g_listviews` objects; if that global ever disappears, the raw `new Listview({...})` script
blocks are parsed instead. Drop chance is `count / outof` (Wowhead's own loot statistics).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .capture import capture_dir


@dataclass(frozen=True)
class ItemSource:
    item_id: int
    kind: str  # drop | skinning | pickpocket | mining | herbalism | object | fishing
    source_type: str  # npc | object | zone
    source_id: int
    source_name: str
    chance: float | None  # per kill / skin / node, 0-1
    samples: int | None  # Wowhead's "outof": how many kills/loots the chance is based on
    zones: tuple[int, ...]
    level: tuple[int, int] | None = None
    # Wowhead "react" per faction (alliance, horde): -1 hostile, 0 neutral, 1 friendly
    react: tuple[int | None, int | None] | None = None
    elite: bool = False
    boss: bool = False
    skill: int | None = None  # gathering skill needed (gathered/mined-from objects)
    item_name: str = ""  # from the captured page's title

    def friendly_to(self, faction: str) -> bool:
        if not self.react:
            return False
        return self.react[0 if faction == "alliance" else 1] == 1


def classify(tab_id: str, template: str | None) -> tuple[str, str] | None:
    """(kind, source_type) for a Listview tab that lists where an item comes from."""
    t = tab_id.lower()
    if "skinn" in t:
        return "skinning", "npc"
    if "pickpocket" in t:
        return "pickpocket", "npc"
    if t.startswith("dropped-by") or t == "drop":
        return "drop", "npc"
    if "mined" in t:
        return "mining", "object"
    if "herb" in t or "gathered" in t:
        return "herbalism", "object"
    if "fished" in t:
        return "fishing", "zone" if template == "zone" else "object"
    if "contained-in-object" in t:
        return "object", "object"
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows_to_sources(
    item_id: int, tab_id: str, template: str | None, rows, item_name: str = ""
) -> list[ItemSource]:
    kind = classify(tab_id, template)
    if kind is None or not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict) or _num(row.get("id")) is None:
            continue
        count, outof = _num(row.get("count")), _num(row.get("outof"))
        chance = None
        if count is not None and outof:
            chance = max(0.0, count) / outof
        elif (pct := _num(row.get("percent"))) is not None:
            chance = pct / 100
        loc = row.get("location") or []
        zones = tuple(
            int(z) for z in (loc if isinstance(loc, list) else [loc]) if (_num(z) or 0) > 0
        )
        lo, hi = _num(row.get("minlevel")), _num(row.get("maxlevel"))
        react = row.get("react")
        react_t = None
        if isinstance(react, list) and len(react) == 2:
            react_t = tuple(int(x) if _num(x) is not None else None for x in react)
        out.append(
            ItemSource(
                item_id=item_id,
                kind=kind[0],
                source_type=kind[1],
                source_id=int(row["id"]),
                source_name=str(row.get("name") or row.get("name_enus") or "").lstrip("0123456789"),
                chance=chance,
                samples=int(outof) if outof else None,
                zones=zones,
                level=(int(lo), int(hi or lo)) if lo else None,
                react=react_t,
                elite=(_num(row.get("classification")) or 0) >= 1,
                boss=bool(row.get("boss")),
                skill=int(sk) if (sk := _num(row.get("skill"))) else None,
                item_name=item_name,
            )
        )
    return out


def _balanced(text: str, start: int) -> str | None:
    """The bracketed JS/JSON value starting at text[start] ('[' or '{')."""
    open_, close = text[start], {"[": "]", "{": "}"}[text[start]]
    depth, i, in_str, quote = 0, start, False, ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def parse_raw_listviews(script: str) -> list[tuple[str, str | None, list]]:
    """(id, template, data) for each `new Listview({...})` whose data is valid JSON."""
    out = []
    for m in re.finditer(r"new Listview\(\s*\{", script):
        body = _balanced(script, m.end() - 1)
        if not body:
            continue
        tab = re.search(r"""\bid\s*:\s*['"]([\w-]+)['"]""", body)
        tmpl = re.search(r"""\btemplate\s*:\s*['"]([\w-]+)['"]""", body)
        data = re.search(r"\bdata\s*:\s*\[", body)
        if not (tab and data):
            continue
        arr = _balanced(body, data.end() - 1)
        try:
            rows = json.loads(arr) if arr else None
        except json.JSONDecodeError:
            continue
        out.append((tab[1], tmpl[1] if tmpl else None, rows))
    return out


def item_name_from_title(title: str) -> str:
    """'Fel Crystal - Item - World of Warcraft' -> 'Fel Crystal'."""
    return title.split(" - ")[0].strip() if " - " in title else ""


def sources_from_capture(record: dict, item_id: int) -> list[ItemSource]:
    name = item_name_from_title(str(record.get("title") or ""))
    found: list[ItemSource] = []
    seen_tabs = set()
    for tab_id, lv in (record.get("listviews") or {}).items():
        if isinstance(lv, dict) and "data" in lv:
            got = _rows_to_sources(item_id, tab_id, lv.get("template"), lv["data"], name)
            if got:
                seen_tabs.add(tab_id)
                found += got
    for script in record.get("raw") or []:
        for tab_id, template, rows in parse_raw_listviews(script):
            if tab_id not in seen_tabs:
                seen_tabs.add(tab_id)
                found += _rows_to_sources(item_id, tab_id, template, rows, name)
    return found


def load_sources(data_dir: Path) -> list[ItemSource]:
    out = []
    for path in sorted(capture_dir(data_dir).glob("item-*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out += sources_from_capture(record, int(path.stem.split("-", 1)[1]))
    return out
