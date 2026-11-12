"""Keeping Wowhead captures fresh.

- A capture is **outdated** when it's older than `stale_days`, or older than the last "new
  patch" mark (set it on patch day: everything captured before is outdated).
- The **recapture queue** puts outdated pages in order of what they're worth: profession and
  item lists first (they cover many items), then NPC/object pages by the value of their drops
  (small Wowhead samples count double, since their rates improve most), then item pages by
  the item's market value.
- **What changed:** when a page is recaptured, its previous version is kept; drop tables are
  compared item by item.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..crafting.recipes import _recipes_in_capture
from .capture import capture_dir, previous_capture
from .itemlists import hints_from_capture
from .sources import ItemSource, sources_from_capture, sources_from_entity_page

DEFAULT_STALE_DAYS = 14
SMALL_SAMPLE = 500
RATE_CHANGE_REL = 0.3  # a drop rate "changed" if it moved by 30% ...
RATE_CHANGE_ABS = 0.005  # ... and at least half a percentage point
MIN_SAMPLES_TO_COMPARE = 30


# -- settings ------------------------------------------------------------------------------


def _settings_path(data_dir: Path) -> Path:
    return data_dir / "wowhead" / "refresh.json"


def load_settings(data_dir: Path) -> dict:
    try:
        s = json.loads(_settings_path(data_dir).read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    return {
        "stale_days": int(s.get("stale_days") or DEFAULT_STALE_DAYS),
        "stale_before": s.get("stale_before"),
    }


def save_settings(
    data_dir: Path,
    *,
    stale_days: int | None = None,
    stale_before: str | None = None,
    clear_mark: bool = False,
) -> dict:
    s = load_settings(data_dir)
    if stale_days is not None:
        s["stale_days"] = max(1, int(stale_days))
    if stale_before is not None:
        s["stale_before"] = stale_before
    if clear_mark:
        s["stale_before"] = None
    path = _settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=2))
    return s


def mark_patch(data_dir: Path, when: datetime | None = None) -> dict:
    """Everything captured before `when` (default: now) is outdated."""
    stamp = (when or datetime.now(UTC)).isoformat(timespec="seconds")
    return save_settings(data_dir, stale_before=stamp)


def stale_reason(
    captured_at: str | None, settings: dict, now: datetime | None = None
) -> str | None:
    if not captured_at:
        return "capture time unknown"
    try:
        at = datetime.fromisoformat(captured_at)
    except ValueError:
        return "capture time unknown"
    if settings.get("stale_before") and captured_at <= settings["stale_before"]:
        return "captured before the last patch mark"
    age = (now or datetime.now(UTC)) - at
    if age > timedelta(days=settings["stale_days"]):
        return f"{age.days} days old"
    return None


# -- what a page says ------------------------------------------------------------------------


def _sources(record: dict, key: str) -> list[ItemSource]:
    kind, _, raw_id = key.partition("-")
    if not raw_id.isdigit():
        return []
    if kind == "item":
        return sources_from_capture(record, int(raw_id))
    if kind in ("npc", "object"):
        return sources_from_entity_page(record, kind, int(raw_id))
    return []


def _rates(record: dict, key: str) -> dict[tuple, tuple[float, int | None, str]]:
    """(source, item) -> (chance, samples, label) for one captured page."""
    out = {}
    for s in _sources(record, key):
        if s.chance is None:
            continue
        label = s.item_name or s.source_name or f"item {s.item_id}"
        if key.startswith("item-"):
            label = s.source_name or f"{s.source_type} {s.source_id}"
        out[(s.source_type, s.source_id, s.kind, s.item_id)] = (s.chance, s.samples, label)
    return out


def _listed_ids(record: dict, key: str) -> set[int]:
    if key.startswith("spells-"):
        return {r.spell_id for r in _recipes_in_capture(record, key)}
    if key.startswith("items-"):
        return {h.item_id for h in hints_from_capture(record)}
    return set()


@dataclass
class Change:
    kind: str  # new | gone | rate | added | removed
    label: str
    old: float | None = None
    new: float | None = None


@dataclass
class PageChanges:
    key: str
    title: str
    captured_at: str
    previous_at: str
    changes: list[Change] = field(default_factory=list)


def page_changes(data_dir: Path, key: str) -> PageChanges | None:
    path = capture_dir(data_dir) / f"{key}.json"
    prev = previous_capture(data_dir, key)
    if prev is None or not path.exists():
        return None
    try:
        cur = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    out = PageChanges(
        key,
        str(cur.get("title") or key),
        str(cur.get("captured_at") or ""),
        str(prev.get("captured_at") or ""),
    )
    if key.startswith(("spells-", "items-")):
        a, b = _listed_ids(prev, key), _listed_ids(cur, key)
        if b - a:
            out.changes.append(Change("added", f"{len(b - a)} new entries"))
        if a - b:
            out.changes.append(Change("removed", f"{len(a - b)} entries gone"))
        return out
    old, new = _rates(prev, key), _rates(cur, key)
    for k, (chance, samples, label) in new.items():
        if k not in old:
            out.changes.append(Change("new", label, None, chance))
            continue
        before, before_n, _ = old[k]
        if (samples or 0) < MIN_SAMPLES_TO_COMPARE or (before_n or 0) < MIN_SAMPLES_TO_COMPARE:
            continue
        diff = abs(chance - before)
        if diff >= RATE_CHANGE_ABS and diff >= RATE_CHANGE_REL * max(before, chance):
            out.changes.append(Change("rate", label, before, chance))
    for k, (chance, _, label) in old.items():
        if k not in new:
            out.changes.append(Change("gone", label, chance, None))
    return out


def recent_changes(data_dir: Path, limit: int = 30) -> list[PageChanges]:
    """Pages whose latest recapture changed something, newest first."""
    hist_root = data_dir / "wowhead" / "history"
    if not hist_root.exists():
        return []
    out = []
    for d in hist_root.iterdir():
        if d.is_dir() and (pc := page_changes(data_dir, d.name)) and pc.changes:
            out.append(pc)
    out.sort(key=lambda pc: pc.captured_at, reverse=True)
    return out[:limit]


# -- recapture queue ---------------------------------------------------------------------------


@dataclass
class Recapture:
    key: str
    title: str
    url: str
    captured_at: str
    reason: str
    priority: float


def recapture_queue(
    data_dir: Path,
    item_value: Callable[[int], float],
    *,
    now: datetime | None = None,
) -> list[Recapture]:
    """Outdated captures, most worth recapturing first. `item_value(item)` is copper per day
    the item trades for (price × daily sales); drop rates scale it per kill."""
    settings = load_settings(data_dir)
    out = []
    for path in sorted(capture_dir(data_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = path.stem
        reason = stale_reason(record.get("captured_at"), settings, now)
        if reason is None:
            continue
        kind, _, raw_id = key.partition("-")
        if kind in ("spells", "items"):
            priority = 1e15  # lists cover many items at once
        elif kind == "item" and raw_id.isdigit():
            priority = item_value(int(raw_id))
        else:
            rates = _rates(record, key)
            priority = sum(chance * item_value(k[3]) for k, (chance, _, _) in rates.items())
            if any((n or 0) < SMALL_SAMPLE for _, n, _ in rates.values()):
                priority *= 2
        out.append(
            Recapture(
                key,
                str(record.get("title") or key),
                str(record.get("url") or ""),
                str(record.get("captured_at") or ""),
                reason,
                priority,
            )
        )
    out.sort(key=lambda r: -r.priority)
    return out
