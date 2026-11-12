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
from ..market.firstseen import new_arrivals
from ..market.myhouse import house_source, load_my_houses
from ..planner.engine import PlanOptions
from ..planner.pricing import PriceBook, build_price_book, cap_prices, pick_houses
from ..planner.profile import CharacterProfile
from ..play.calibration import Calibration
from ..play.calibration import build as build_calibration
from ..wowhead.capture import captured
from ..wowhead.entities import load_entities
from ..wowhead.itemlists import load_item_hints
from ..wowhead.names import captured_item_names, store_names
from ..wowhead.sources import ItemSource, load_sources
from ..wowhead.vendors import VendorOffer, load_vendor_offers
from . import vanilla
from .discover import (
    Candidate,
    Lead,
    VanillaData,
    candidate_item_ids,
    find_farms,
    find_leads,
    forever_camps,
    profile_at_level,
)
from .newitems import Overview, build_overview, own_loot


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
    capped: dict[int, VendorOffer] = field(default_factory=dict)  # AH prices capped by vendors
    house_source: str = "guess"  # chosen | configured | confirmed | guess
    calibration: Calibration | None = None  # from your recorded sessions

    @property
    def hidden_elite_leads(self) -> int:
        return len({(u.source_type, u.source_id) for u in self.unplaced if u.elite})


def _caps(offers: dict[int, VendorOffer]) -> dict[int, float]:
    return {i: o.price for i, o in offers.items() if not o.limited}


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
    mine = load_my_houses(cache.data_dir, region)
    house_ids = pick_houses(conn, region, profile.faction, house, configured_houses, mine)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    # Your recorded sessions: measured kill speed and respawn times
    calibration = build_calibration(conn, data)
    data.respawn = {npc: r.seconds for npc, r in calibration.respawns.items()}
    profile = profile.model_copy(update={"kill_speed": calibration.speed(profile.cls)})
    recipes, ctx = [], None
    if craft:
        recipes, ctx, _ = cache.crafting()
        item_ids = item_ids | {i for r in recipes for i in r.item_ids}
    book = build_price_book(conn, region, house_ids, item_ids)
    offers = load_vendor_offers(cache.data_dir)
    capped = cap_prices(book, _caps(offers))
    if ctx is not None:
        ctx = ctx.with_offers(offers)
    craft_opts = CraftOptions(sell_days=opts.sell_days, market_share=opts.market_share)
    entities = load_entities(cache.data_dir)
    # Forever zones aren't in the vanilla area table; Wowhead's map data names them
    zone_names = {
        p.zone_id: p.zone_name
        for e in entities.values()
        for p in e.points
        if p.zone_id is not None and p.zone_name
    }
    out = DiscoverResult(
        profile=profile,
        house_ids=house_ids,
        placed=placed,
        unplaced=unplaced,
        sales_coverage_h=book.sales_coverage_h,
        sweep=bool(levels),
        book=book,
        capped={i: offers[i] for i in capped},
        house_source=house_source(
            house_ids, profile.faction, explicit=house, configured=configured_houses, mine=mine
        ),
        calibration=calibration,
    )
    for level in levels or [profile.level]:
        at = profile_at_level(profile, level) if levels else profile
        fcamps = forever_camps(data, at, book, entities, unplaced, elites=elites)
        camp_npcs = {c.pages[0][1] for c in fcamps}
        farms = find_farms(data, at, book, opts, kind=kind, elites=elites, extra=fcamps)
        if craft and ctx is not None:
            proc = processing_options(recipes, book, ctx, craft_opts, at.professions)
            for c in farms:
                c.craft_extra, c.craft_notes = farm_craft_uplift(
                    c.evaluation.lines, proc, hours=opts.hours, sell_days=opts.sell_days
                )
            farms.sort(key=lambda c: -c.total_per_hour)
        farms = farms[:top]
        lead_sources = [
            u for u in unplaced if not (u.source_type == "npc" and u.source_id in camp_npcs)
        ]
        found = find_leads(
            data,
            lead_sources,
            at,
            book,
            opts,
            elites=elites,
            entities=entities,
            zone_names=zone_names,
        )[:leads]
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
    capped: dict[int, VendorOffer] = field(default_factory=dict)
    house_source: str = "guess"


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
    mine = load_my_houses(cache.data_dir, region)
    house_ids = pick_houses(conn, region, faction, house, configured_houses, mine)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    item_ids = {i for r in recipes for i in r.item_ids}
    item_ids |= {ls.item_id for r in recipes for ls in r.learn if ls.item_id}
    book = build_price_book(conn, region, house_ids, item_ids)
    offers = load_vendor_offers(cache.data_dir)
    capped = cap_prices(book, _caps(offers))
    ctx = ctx.with_offers(offers)
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
        capped={i: offers[i] for i in capped},
        house_source=house_source(
            house_ids, faction, explicit=house, configured=configured_houses, mine=mine
        ),
    )


@dataclass
class NewItemsResult:
    overview: Overview
    house_ids: list[int]
    house_source: str
    names_added: int
    book: PriceBook | None = None


def run_new_items(
    cache: VanillaCache,
    conn: sqlite3.Connection,
    *,
    faction: str,
    region: str,
    house: list[int] | None = None,
    configured_houses: list[int] | None = None,
) -> NewItemsResult:
    """Every Forever-only item we know of, with its price and where it comes from."""
    data, _ids, placed, unplaced = cache.with_captures()
    recipes, _ctx, _ = cache.crafting()
    names = captured_item_names(cache.data_dir)
    names_added = store_names(conn, names)
    mine = load_my_houses(cache.data_dir, region)
    house_ids = pick_houses(conn, region, faction, house, configured_houses, mine)
    if not house_ids:
        raise LookupError("no auction data for that faction; run `wowgold collect --once`")
    marks = ",".join("?" * len(house_ids))
    ah_items = {
        r[0]
        for r in conn.execute(
            f"""SELECT DISTINCT item_id FROM price_stats WHERE snapshot_id IN (
                    SELECT MAX(id) FROM snapshots WHERE region = ? AND house_id IN ({marks})
                      AND outcome = 'stored' AND ingested_at IS NOT NULL GROUP BY house_id, kind)
                  AND quantity > 0""",
            (region, *house_ids),
        )
    }
    offers = load_vendor_offers(cache.data_dir)
    hints = load_item_hints(cache.data_dir)
    item_ids = ah_items | {s.item_id for s in placed + unplaced} | set(offers) | set(hints)
    item_ids |= {i for r in recipes if r.source == "wowhead" for i in r.item_ids}
    book = build_price_book(conn, region, house_ids, item_ids)
    cap_prices(book, _caps(offers))
    overview = build_overview(
        data,
        book,
        ah_items=ah_items,
        placed=placed,
        unplaced=unplaced,
        recipes=recipes,
        offers=offers,
        hints=hints,
        names={i: n.name for i, n in names.items()},
        captured=set(captured(cache.data_dir)),
        yours=own_loot(conn),
        arrivals=new_arrivals(conn, region),
    )
    return NewItemsResult(
        overview=overview,
        book=book,
        house_ids=house_ids,
        house_source=house_source(
            house_ids, faction, explicit=house, configured=configured_houses, mine=mine
        ),
        names_added=names_added,
    )
