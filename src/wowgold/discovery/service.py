"""Run `discover` for a character: shared by the CLI and the web app."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..crafting.profit import (
    CraftContext,
    CraftEval,
    CraftOptions,
    Processing,
    farm_craft_uplift,
    processing_options,
    rank_crafts,
)
from ..crafting.recipes import Recipe, load_vanilla_recipes, load_wowhead_recipes, merge_recipes
from ..planner.engine import PlanOptions
from ..planner.pricing import PriceBook, build_price_book, pick_houses
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
        self._recipes: dict[int, Recipe] = {}
        self._craft_ctx: CraftContext | None = None

    @property
    def available(self) -> bool:
        return vanilla.db_path(self.data_dir).exists()

    def _base(self) -> tuple[VanillaData, set[int]]:
        path = vanilla.db_path(self.data_dir)
        mtime = path.stat().st_mtime  # FileNotFoundError if missing
        with self._lock:
            if self._data is None or mtime != self._mtime:
                conn = vanilla.connect(path)
                data = VanillaData.load(conn)
                # Resolves every loot table now, so later requests never touch the DB.
                self._item_ids = candidate_item_ids(data)
                self._recipes = load_vanilla_recipes(conn)
                vendor = frozenset(
                    r[0] for r in conn.execute("SELECT DISTINCT item FROM npc_vendor")
                )
                self._craft_ctx = CraftContext(
                    vendor_items=vendor,
                    vendor_price={i: data.items[i].buy_price for i in vendor if i in data.items},
                    items=data.items,
                )
                self._data, self._mtime = data, mtime
            return self._data, self._item_ids

    def crafting(self) -> tuple[list[Recipe], CraftContext, int]:
        """All recipes (vanilla + Forever ones from captures), pricing context, and how many
        came from captures."""
        self._base()
        forever = load_wowhead_recipes(self.data_dir)
        recipes = merge_recipes(self._recipes, forever)
        assert self._craft_ctx is not None
        return list(recipes.values()), self._craft_ctx, len(forever)

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
    book: PriceBook | None = None  # prices and sell rates the farms were scored with

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
    craft: bool = False,
) -> DiscoverResult:
    """Rank farms (and leads) for one character, or for each level of a sweep.

    Raises FileNotFoundError without the vanilla DB, LookupError without AH data."""
    data, item_ids, placed, unplaced = cache.with_captures()
    house_ids = pick_houses(conn, region, profile.faction, house, configured_houses)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    recipes, ctx = [], None
    if craft:
        recipes, ctx, _ = cache.crafting()
        item_ids = item_ids | {i for r in recipes for i in r.item_ids}
    book = build_price_book(conn, region, house_ids, item_ids)
    craft_opts = CraftOptions(sell_days=opts.sell_days, market_share=opts.market_share)
    out = DiscoverResult(
        profile=profile,
        house_ids=house_ids,
        placed=placed,
        unplaced=unplaced,
        sales_coverage_h=book.sales_coverage_h,
        sweep=bool(levels),
        book=book,
    )
    for level in levels or [profile.level]:
        at = profile_at_level(profile, level) if levels else profile
        farms = find_farms(data, at, book, opts, kind=kind, elites=elites)
        if craft and ctx is not None:
            proc = processing_options(recipes, book, ctx, craft_opts, at.professions)
            for c in farms:
                c.craft_extra, c.craft_notes = farm_craft_uplift(
                    c.evaluation.lines, proc, hours=opts.hours, sell_days=opts.sell_days
                )
            farms.sort(key=lambda c: -c.total_per_hour)
        farms = farms[:top]
        found = find_leads(data, unplaced, at, book, opts, elites=elites)[:leads]
        out.results.append(LevelResult(at, farms, found))
    return out


@dataclass
class CraftingResult:
    house_ids: list[int]
    crafts: list[CraftEval]
    processing: list[Processing]
    book: PriceBook
    recipes_total: int
    recipes_forever: int
    sales_coverage_h: float


def run_crafting(
    cache: VanillaCache,
    conn: sqlite3.Connection,
    *,
    faction: str,
    professions: dict[str, int],
    opts: CraftOptions,
    region: str,
    house: list[int] | None = None,
    configured_houses: list[int] | None = None,
    only_craftable: bool = True,
    min_sales: float = 0.0,
    cooldowns: bool = True,
    top: int = 50,
) -> CraftingResult:
    """Profitable crafts from bought materials, and the best way to process materials you
    bring yourself. Raises FileNotFoundError without the vanilla DB, LookupError without AH
    data."""
    recipes, ctx, n_forever = cache.crafting()
    house_ids = pick_houses(conn, region, faction, house, configured_houses)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    item_ids = {i for r in recipes for i in r.item_ids}
    item_ids |= {ls.item_id for r in recipes for ls in r.learn if ls.item_id}
    book = build_price_book(conn, region, house_ids, item_ids)
    crafts = rank_crafts(
        recipes,
        book,
        ctx,
        opts,
        professions=professions if only_craftable else None,
        min_sales=min_sales,
        cooldowns=cooldowns,
    )[:top]
    proc = processing_options(recipes, book, ctx, opts, professions)
    processing = sorted(proc.values(), key=lambda p: -p.uplift * p.units_per_day)[:top]
    return CraftingResult(
        house_ids=house_ids,
        crafts=crafts,
        processing=processing,
        book=book,
        recipes_total=len(recipes),
        recipes_forever=n_forever,
        sales_coverage_h=book.sales_coverage_h,
    )
