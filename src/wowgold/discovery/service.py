"""Run `discover` for a character: shared by the CLI and the web app."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..planner.engine import PlanOptions
from ..planner.pricing import build_price_book, pick_houses
from ..planner.profile import CharacterProfile
from ..wowhead.sources import ItemSource, load_sources
from . import vanilla
from .discover import (
    Candidate,
    Lead,
    VanillaData,
    candidate_item_ids,
    find_farms,
    find_leads,
    profile_at_level,
)


class VanillaCache:
    """Loads the vanilla world data once (and again if the file changes). Each call to
    `with_captures` returns a copy with the current Wowhead captures merged in, so captures
    saved while the app runs are picked up without reloading everything."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._data: VanillaData | None = None
        self._item_ids: set[int] = set()
        self._mtime: float | None = None

    @property
    def available(self) -> bool:
        return vanilla.db_path(self.data_dir).exists()

    def _base(self) -> tuple[VanillaData, set[int]]:
        path = vanilla.db_path(self.data_dir)
        mtime = path.stat().st_mtime  # FileNotFoundError if missing
        with self._lock:
            if self._data is None or mtime != self._mtime:
                data = VanillaData.load(vanilla.connect(path))
                # Resolves every loot table now, so later requests never touch the DB.
                self._item_ids = candidate_item_ids(data)
                self._data, self._mtime = data, mtime
            return self._data, self._item_ids

    def with_captures(self) -> tuple[VanillaData, set[int], list[ItemSource], list[ItemSource]]:
        base, item_ids = self._base()
        data = dataclasses.replace(base, extra={}, extra_notes={})
        placed, unplaced = data.add_sources(load_sources(self.data_dir))
        ids = set(item_ids) | {s.item_id for s in placed} | {u.item_id for u in unplaced}
        return data, ids, placed, unplaced


@dataclass
class LevelResult:
    profile: CharacterProfile
    farms: list[Candidate]
    leads: list[Lead]


@dataclass
class DiscoverResult:
    profile: CharacterProfile
    house_ids: list[int]
    results: list[LevelResult] = field(default_factory=list)
    placed: list[ItemSource] = field(default_factory=list)
    unplaced: list[ItemSource] = field(default_factory=list)
    sales_coverage_h: float = 0.0
    sweep: bool = False

    @property
    def hidden_elite_leads(self) -> int:
        return len({(u.source_type, u.source_id) for u in self.unplaced if u.elite})


def run_discover(
    cache: VanillaCache,
    conn: sqlite3.Connection,
    profile: CharacterProfile,
    opts: PlanOptions,
    *,
    region: str,
    house: list[int] | None = None,
    configured_houses: list[int] | None = None,
    kind: str = "all",
    elites: bool = False,
    levels: list[int] | None = None,
    top: int = 15,
    leads: int = 8,
) -> DiscoverResult:
    """Rank farms (and leads) for one character, or for each level of a sweep.

    Raises FileNotFoundError without the vanilla DB, LookupError without AH data."""
    data, item_ids, placed, unplaced = cache.with_captures()
    house_ids = pick_houses(conn, region, profile.faction, house, configured_houses)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    book = build_price_book(conn, region, house_ids, item_ids)
    out = DiscoverResult(
        profile=profile,
        house_ids=house_ids,
        placed=placed,
        unplaced=unplaced,
        sales_coverage_h=book.sales_coverage_h,
        sweep=bool(levels),
    )
    for level in levels or [profile.level]:
        at = profile_at_level(profile, level) if levels else profile
        farms = find_farms(data, at, book, opts, kind=kind, elites=elites)[:top]
        found = find_leads(data, unplaced, at, book, opts, elites=elites)[:leads]
        out.results.append(LevelResult(at, farms, found))
    return out
