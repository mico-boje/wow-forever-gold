"""Recorded sessions, digested and valued at today's prices, for the web UI and CLI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..market.myhouse import load_my_houses
from ..planner.pricing import PriceBook, build_price_book, pick_houses
from ..wowhead.entities import load_entities
from .calibration import Calibration, build
from .digest import Digest, digest
from .recordings import Recording, load


@dataclass
class SessionsView:
    digests: list[Digest]
    calibration: Calibration
    books: dict[str, PriceBook] = field(default_factory=dict)  # by faction
    vanilla: bool = True  # vanilla data available (names, levels, the loot model)
    names: dict[int, str] = field(default_factory=dict)  # mob names for the calibration list


def _loot_items(d: Digest, data) -> set[int]:
    from ..discovery.discover import creature_loot, skinning_loot

    ids = set(d.items)
    if data is not None:
        for s in d.mobs:
            c = data.creatures.get(s.id)
            if c is not None:
                ids |= set(creature_loot(c, data)) | set(skinning_loot(c, data))
    return ids


def sessions_view(
    conn: sqlite3.Connection,
    cache,  # discovery.service.VanillaCache
    region: str,
    configured_houses: list[int] | None = None,
    *,
    rid: str | None = None,
) -> SessionsView:
    data = cache.with_captures()[0] if cache.available else None
    names = {
        eid: e.info.name
        for (kind, eid), e in load_entities(cache.data_dir).items()
        if kind == "npc" and e.info and e.info.name
    }
    recs: list[Recording] = load(conn, rid=rid)
    raw = [digest(r, data=data, names=names) for r in recs]
    # price everything a faction's sessions looted (and what the loot model expects)
    by_faction: dict[str, set[int]] = {}
    for d in raw:
        fac = d.rec.faction or "horde"
        by_faction.setdefault(fac, set()).update(_loot_items(d, data))
    mine = load_my_houses(cache.data_dir, region)
    books = {}
    for fac, ids in by_faction.items():
        houses = pick_houses(conn, region, fac, None, configured_houses, mine)
        if houses:
            books[fac] = build_price_book(conn, region, houses, ids)
    digests = [
        digest(r, data=data, book=books.get(r.faction or "horde"), names=names) for r in recs
    ]
    cal = build(conn, data, names)
    mob_names = {
        npc: (data.creatures[npc].name if data is not None and npc in data.creatures else None)
        or names.get(npc)
        or f"NPC {npc}"
        for npc in cal.respawns
    }
    return SessionsView(digests, cal, books, data is not None, mob_names)
