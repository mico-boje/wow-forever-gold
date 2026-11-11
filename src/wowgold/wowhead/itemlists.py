"""Where items come from, according to Wowhead's item list pages.

Each row of an item list (e.g. /forever/items/trade-goods) says how the item is obtained
("source": drop, crafted, vendor, ...) and often names the mob, object or recipe
("sourcemore"). There's no drop chance here, but it tells you which NPC page to capture next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .capture import capture_dir
from .sources import parse_raw_listviews

# Wowhead "source" codes on item rows
SOURCE_KINDS = {
    1: "crafted",
    2: "drop",
    3: "pvp",
    4: "quest",
    5: "vendor",
    6: "trainer",
    7: "discovery",
    10: "starter",
    12: "achievement",
    16: "fishing",
}
# Wowhead object types in "sourcemore": t
TYPES = {1: "npc", 2: "object", 3: "item", 5: "quest", 6: "spell", 7: "zone"}


@dataclass(frozen=True)
class SourceRef:
    kind: str  # drop | crafted | vendor | quest | ...
    type: str | None  # npc | object | spell | zone | ...
    ref_id: int | None
    name: str


@dataclass
class ItemHint:
    item_id: int
    name: str
    quality: int | None
    level: int | None
    kinds: list[str] = field(default_factory=list)
    refs: list[SourceRef] = field(default_factory=list)


def _hint(row: dict) -> ItemHint | None:
    if not isinstance(row.get("id"), int) or not ("classs" in row or "slot" in row):
        return None
    kinds = [
        SOURCE_KINDS.get(c, f"source {c}") for c in row.get("source") or [] if isinstance(c, int)
    ]
    refs = []
    for m in row.get("sourcemore") or []:
        if not isinstance(m, dict):
            continue
        t = TYPES.get(m.get("t")) if isinstance(m.get("t"), int) else None
        kind = kinds[0] if kinds else "unknown"
        if t == "npc":
            kind = "drop" if "drop" in kinds else kind
        elif t == "spell":
            kind = "crafted"
        refs.append(
            SourceRef(
                kind,
                t,
                m.get("ti") if isinstance(m.get("ti"), int) else None,
                str(m.get("n") or "").lstrip("0123456789"),
            )
        )
    name = str(row.get("name") or "")
    quality = int(name[0]) if name[:1].isdigit() else row.get("quality")
    return ItemHint(
        item_id=row["id"],
        name=name.lstrip("0123456789"),
        quality=quality if isinstance(quality, int) else None,
        level=row.get("level") if isinstance(row.get("level"), int) else None,
        kinds=kinds,
        refs=refs,
    )


def hints_from_capture(record: dict) -> list[ItemHint]:
    tables = [
        lv.get("data") or []
        for lv in (record.get("listviews") or {}).values()
        if isinstance(lv, dict)
    ]
    seen = {
        t
        for t, lv in (record.get("listviews") or {}).items()
        if isinstance(lv, dict) and lv.get("data")
    }
    for script in record.get("raw") or []:
        tables += [rows for tab, _, rows in parse_raw_listviews(script) if tab not in seen]
    out = []
    for rows in tables:
        for row in rows:
            if isinstance(row, dict) and (h := _hint(row)):
                out.append(h)
    return out


def load_item_hints(data_dir: Path) -> dict[int, ItemHint]:
    """Hints from every captured item list page (and the 'see also' tables of item pages)."""
    out: dict[int, ItemHint] = {}
    for path in sorted(capture_dir(data_dir).glob("items-*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for h in hints_from_capture(record):
            cur = out.get(h.item_id)
            if cur is None or (not cur.refs and h.refs):
                out[h.item_id] = h
    return out


def list_page_counts(data_dir: Path) -> dict[str, int]:
    counts = {}
    for path in sorted(capture_dir(data_dir).glob("items-*.json")):
        try:
            counts[path.stem] = len(hints_from_capture(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError):
            counts[path.stem] = 0
    return counts
