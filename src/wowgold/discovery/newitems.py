"""Everything new in Forever, in one list: what each item is worth and where it comes from.

An item counts as new when it isn't in the vanilla database. Sources are gathered from every
place wowgold looks:

- captured drop tables placed on vanilla mobs/nodes (a farm can be built: "camp"),
- captured sources without open-world spawns ("lead"),
- Forever recipes that make it ("crafted"),
- Forever vendors in captures ("vendor"),
- Wowhead item lists, which name a source but no drop rate ("listed"),
- your own play, from the addon's loot log ("yours").
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from ..crafting.recipes import Recipe
from ..planner.pricing import PriceBook
from ..segments import parse_segment
from ..wowhead.capture import WOWHEAD
from ..wowhead.itemlists import ItemHint
from ..wowhead.sources import ItemSource
from ..wowhead.vendors import VendorOffer
from .discover import VanillaData, unit_value

STATUS_ORDER = ("camp", "lead", "crafted", "vendor", "listed", "yours", "unknown")
STATUS_LABEL = {
    "camp": "farmable",
    "lead": "lead",
    "crafted": "crafted",
    "vendor": "vendor",
    "listed": "source known, no rate",
    "yours": "seen in your play",
    "unknown": "source unknown",
}


@dataclass
class SourceLine:
    kind: str  # camp | lead | crafted | vendor | listed | yours
    text: str
    chance: float | None = None
    samples: int | None = None
    page: str | None = None  # capture key of the page with its drop rates (npc-123 ...)
    url: str | None = None


@dataclass
class NewItem:
    item_id: int
    name: str
    quality: str | None
    market: int | None
    listed: int
    sold_per_day: float | None  # measured, else estimated from listings
    measured: bool
    sources: list[SourceLine] = field(default_factory=list)
    used_in: int = 0  # Forever recipes that use it
    first_seen: str | None = None  # first listed on your AH, if recently (a new arrival)

    @property
    def is_new(self) -> bool:
        return self.first_seen is not None

    @property
    def status(self) -> str:
        kinds = {s.kind for s in self.sources}
        return next((k for k in STATUS_ORDER if k in kinds), "unknown")

    @property
    def value_per_day(self) -> float:
        return (self.market or 0) * (self.sold_per_day or 0)

    @property
    def url(self) -> str:
        return f"{WOWHEAD}/item={self.item_id}"


@dataclass
class NewDrop:
    """A Forever item dropping from a mob that exists in vanilla (so camps can be built)."""

    mob: str
    npc_id: int
    item: str
    item_id: int
    chance: float
    samples: int | None
    value_per_kill: float


@dataclass
class PageToCapture:
    key: str
    kind: str  # npc | object
    ref_id: int
    name: str
    items: list[str]
    value_per_day: float

    @property
    def url(self) -> str:
        return f"{WOWHEAD}/{self.kind}={self.ref_id}"


@dataclass
class Overview:
    items: list[NewItem]
    new_drops: list[NewDrop]
    to_capture: list[PageToCapture]
    counts: dict[str, int]
    unnamed: int


def _rate(p) -> float | None:
    if not p.market:
        return None
    return p.sold_per_day if p.absorb_measured else p.absorb_per_day


def own_loot(conn: sqlite3.Connection) -> dict[int, list[tuple[str, int | None, int, int]]]:
    """item -> [(source kind, object id, loots, quantity)] from imported addon segments."""
    out: dict[int, dict[tuple[str, int | None], list[int]]] = defaultdict(dict)
    for (payload,) in conn.execute("SELECT payload FROM addon_segments"):
        try:
            seg = parse_segment(json.loads(payload))
        except (ValueError, KeyError, TypeError):
            continue
        for src in seg.sources:
            for item, qty in src.items.items():
                cur = out[item].setdefault((src.kind, src.obj_id), [0, 0])
                cur[0] += src.loots
                cur[1] += qty
    return {
        item: [(k, oid, lq[0], lq[1]) for (k, oid), lq in by_src.items()]
        for item, by_src in out.items()
    }


def build_overview(
    data: VanillaData,
    book: PriceBook,
    *,
    ah_items: set[int],
    placed: list[ItemSource],
    unplaced: list[ItemSource],
    recipes: list[Recipe],
    offers: dict[int, VendorOffer],
    hints: dict[int, ItemHint],
    names: dict[int, str],
    captured: set[str],
    yours: dict[int, list[tuple[str, int | None, int, int]]],
    arrivals: dict[int, str] | None = None,
) -> Overview:
    vanilla = set(data.items)
    forever_recipes = [r for r in recipes if r.source == "wowhead"]
    ids = set(ah_items)
    ids |= {s.item_id for s in placed} | {s.item_id for s in unplaced}
    ids |= {i for r in forever_recipes for i in r.item_ids}
    ids |= set(offers) | set(hints) | set(yours)
    ids -= vanilla

    by_item: dict[int, NewItem] = {}

    def item(i: int) -> NewItem:
        if i not in by_item:
            p = book.get(i)
            measured = p.absorb_measured
            hint = hints.get(i)
            by_item[i] = NewItem(
                item_id=i,
                name=p.name or names.get(i) or (hint.name if hint else "") or f"item {i}",
                quality=None,
                market=p.market,
                listed=p.listed_qty,
                sold_per_day=_rate(p),
                measured=measured,
            )
        return by_item[i]

    for i in ids:
        item(i)

    def mob_name(src: ItemSource) -> str:
        if src.source_type == "npc" and src.source_id in data.creatures:
            return data.creatures[src.source_id].name
        if src.source_type == "object" and src.source_id in data.node_types:
            return data.node_types[src.source_id].name
        return src.source_name or f"{src.source_type} {src.source_id}"

    verbs = {"drop": "Drops from", "skinning": "Skinned from", "pickpocket": "Pickpocketed from"}
    new_drops = []
    for src, kind in [(s, "camp") for s in placed] + [(s, "lead") for s in unplaced]:
        if src.item_id not in by_item or not src.chance:
            continue
        verb = verbs.get(src.kind, "Gathered from")
        where = "" if kind == "camp" else " (no open-world spawns)"
        page = f"{src.source_type}-{src.source_id}"
        by_item[src.item_id].sources.append(
            SourceLine(
                kind,
                f"{verb} {mob_name(src)}{where}",
                src.chance,
                src.samples,
                page,
                f"{WOWHEAD}/{src.source_type}={src.source_id}",
            )
        )
        if kind == "camp" and src.source_type == "npc" and src.kind == "drop":
            new_drops.append(
                NewDrop(
                    mob_name(src),
                    src.source_id,
                    by_item[src.item_id].name,
                    src.item_id,
                    src.chance,
                    src.samples,
                    src.chance * unit_value(src.item_id, data, book),
                )
            )

    for r in forever_recipes:
        if r.output in by_item:
            by_item[r.output].sources.append(
                SourceLine(
                    "crafted", f"Crafted: {r.name} ({r.profession.replace('_', ' ')} {r.skill})"
                )
            )
        for reagent, _ in r.reagents:
            if reagent in by_item:
                by_item[reagent].used_in += 1

    for i, o in offers.items():
        if i in by_item:
            limit = ", limited stock" if o.limited else ""
            by_item[i].sources.append(
                SourceLine("vendor", f"Sold by {o.vendor} for {o.price / 10000:.2f}g{limit}")
            )

    to_capture: dict[str, PageToCapture] = {}
    for i, h in hints.items():
        if i not in by_item:
            continue
        it = by_item[i]
        known_pages = {s.page for s in it.sources if s.page}
        for ref in h.refs:
            if ref.type in ("npc", "object") and ref.ref_id:
                key = f"{ref.type}-{ref.ref_id}"
                if key in known_pages:
                    continue
                it.sources.append(
                    SourceLine(
                        "listed",
                        f"Wowhead: {ref.kind} from {ref.name}",
                        page=key,
                        url=f"{WOWHEAD}/{ref.type}={ref.ref_id}",
                    )
                )
                if key not in captured:
                    page = to_capture.setdefault(
                        key, PageToCapture(key, ref.type, ref.ref_id, ref.name, [], 0.0)
                    )
                    page.items.append(it.name)
                    page.value_per_day += it.value_per_day
            elif ref.type == "spell" and not any(s.kind == "crafted" for s in it.sources):
                it.sources.append(SourceLine("listed", f"Wowhead: crafted ({ref.name})"))
        if not h.refs and h.kinds and not it.sources:
            it.sources.append(SourceLine("listed", "Wowhead: " + ", ".join(h.kinds)))

    for i, seen in yours.items():
        if i not in by_item:
            continue
        for kind, oid, loots, qty in seen:
            what = {"Creature": "npc", "GameObject": "object"}.get(kind, kind)
            name = (
                data.creatures[oid].name
                if what == "npc" and oid in data.creatures
                else (f"{what} {oid}" if oid else "unknown source")
            )
            by_item[i].sources.append(
                SourceLine(
                    "yours",
                    f"You looted {qty} from {name} ({loots} loots)",
                    qty / loots if loots else None,
                    loots,
                )
            )

    for i, ts in (arrivals or {}).items():
        if i in by_item:
            by_item[i].first_seen = ts
    items = sorted(by_item.values(), key=lambda it: (-it.value_per_day, -(it.market or 0)))
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it.status] += 1
        if it.is_new:
            counts["new"] += 1
    new_drops.sort(key=lambda d: -d.value_per_kill)
    unnamed = sum(1 for it in items if it.name.startswith("item "))
    return Overview(
        items=items,
        new_drops=new_drops,
        to_capture=sorted(to_capture.values(), key=lambda p: -p.value_per_day),
        counts=dict(counts),
        unnamed=unnamed,
    )
