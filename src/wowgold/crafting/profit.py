"""What crafting is worth at current AH prices.

- `evaluate_recipe`: buy every reagent (AH, or a vendor when cheaper), craft, sell the output
  after the AH cut. Profit per craft, and per day once limited by how fast the output sells,
  how many reagents are listed right now, and the recipe's cooldown.
- `processing_options`: for each item you could bring yourself (e.g. herbs you farmed), the
  best recipe to turn it into something worth more than selling it raw.
- `farm_craft_uplift`: how much a farm gains if its yields are crafted before selling.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..discovery.vanilla import VItem
from ..planner.pricing import PriceBook, trend_flags
from .recipes import Recipe

# Upper bound on crafts per day when nothing else limits it (vendor-sold output, vendor mats):
# roughly an hour of casting and mailbox runs.
MAX_CRAFTS_PER_DAY = 200


@dataclass(frozen=True)
class CraftOptions:
    sell_days: float = 3.0
    market_share: float = 0.5  # share of the output's daily sales you expect to get


@dataclass(frozen=True)
class CraftContext:
    """Static data needed to price crafts."""

    vendor_items: frozenset[int]  # items vendors sell (unlimited supply)
    vendor_price: dict[int, int]  # item -> vendor price (what you pay)
    items: dict[int, VItem]  # vanilla item metadata (quality/class), may be partial
    vendor_names: dict[int, str] = field(default_factory=dict)  # Forever vendors, by item

    def with_offers(self, offers) -> CraftContext:
        """Add Forever vendor offers (unlimited stock) captured from Wowhead."""
        unlimited = {i: o for i, o in offers.items() if not o.limited}
        prices = dict(self.vendor_price)
        names = dict(self.vendor_names)
        for i, o in unlimited.items():
            if i not in prices or o.price < prices[i] or i not in self.vendor_items:
                prices[i] = round(o.price)
                names[i] = o.vendor
        return replace(
            self,
            vendor_items=self.vendor_items | frozenset(unlimited),
            vendor_price=prices,
            vendor_names=names,
        )


@dataclass
class ReagentLine:
    item_id: int
    name: str
    qty: float
    unit_cost: float | None  # copper; None = can't be bought
    source: str  # AH | vendor | missing
    listed: int

    @property
    def cost(self) -> float | None:
        return None if self.unit_cost is None else self.unit_cost * self.qty


@dataclass
class CraftEval:
    recipe: Recipe
    output_name: str
    reagents: list[ReagentLine]
    cost: float | None  # copper per craft
    output_unit: float  # net copper per output item (after AH cut, or vendor)
    output_on_ah: bool
    output_sold_per_day: float | None
    output_measured: bool
    crafts_demand: float  # crafts/day the output market absorbs (your share)
    crafts_mats: float | None  # crafts the currently listed reagents allow (None = unlimited)
    crafts_cooldown: float | None  # crafts/day the cooldown allows
    learn_cost: int | None
    learn_label: str
    flags: list[str] = field(default_factory=list)

    @property
    def output_value(self) -> float:
        return self.output_unit * self.recipe.output_qty

    @property
    def profit(self) -> float | None:
        return None if self.cost is None else self.output_value - self.cost

    @property
    def margin(self) -> float | None:
        if self.cost is None or self.cost <= 0 or self.profit is None:
            return None
        return self.profit / self.cost

    @property
    def crafts_per_day(self) -> float:
        caps = [self.crafts_demand, MAX_CRAFTS_PER_DAY]
        caps += [c for c in (self.crafts_mats, self.crafts_cooldown) if c is not None]
        return max(0.0, min(caps))

    @property
    def limited_by(self) -> str:
        n = self.crafts_per_day
        if self.crafts_cooldown is not None and n == self.crafts_cooldown:
            return "cooldown"
        if self.crafts_mats is not None and n == self.crafts_mats:
            return "mats listed"
        if n == MAX_CRAFTS_PER_DAY:
            return "crafting time"
        return "demand"

    @property
    def profit_per_day(self) -> float:
        p = self.profit
        return 0.0 if p is None or p <= 0 else p * self.crafts_per_day

    @property
    def payback_crafts(self) -> float | None:
        p = self.profit
        if not self.learn_cost or p is None or p <= 0:
            return None
        return self.learn_cost / p


def _name(item_id: int, book: PriceBook, ctx: CraftContext) -> str:
    price = book.get(item_id)
    vi = ctx.items.get(item_id)
    return price.name or (vi.name if vi else f"item {item_id}")


def vendor_only(vi: VItem | None) -> bool:
    """Soulbound items and white/grey weapons and armor: valued at vendor price only."""
    return vi is not None and (vi.bind == 1 or (vi.quality <= 1 and vi.iclass in (2, 4)))


def buy_price(item_id: int, book: PriceBook, ctx: CraftContext) -> tuple[float | None, str, int]:
    """(unit cost, source, listed): the cheaper of the AH and a vendor."""
    price = book.get(item_id)
    vendor = ctx.vendor_price.get(item_id) if item_id in ctx.vendor_items else None
    ah = price.market
    if vendor and (ah is None or vendor <= ah):
        name = ctx.vendor_names.get(item_id)
        return float(vendor), f"vendor ({name})" if name else "vendor", price.listed_qty
    if ah:
        return float(ah), "AH", price.listed_qty
    return None, "missing", 0


def sell_unit(item_id: int, book: PriceBook, ctx: CraftContext) -> tuple[float, bool]:
    """(net copper per unit, sold on the AH?)."""
    price = book.get(item_id)
    vendor = price.vendor_sell or (ctx.items[item_id].sell_price if item_id in ctx.items else 0)
    if vendor_only(ctx.items.get(item_id)) or not price.market:
        return float(vendor), False
    net = price.ah_net  # sells at the trend-adjusted price
    return (net, True) if net > vendor else (float(vendor), False)


def _learn(recipe: Recipe, book: PriceBook) -> tuple[int | None, str]:
    options: list[tuple[int, str]] = []
    unknown = []
    for ls in recipe.learn:
        if ls.kind == "starter":
            options.append((0, "known from the start"))
        elif ls.kind == "trainer":
            options.append((ls.cost or 0, "trainer"))
        elif ls.kind == "recipe" and ls.item_id:
            if ls.vendor and ls.cost is not None:
                options.append((ls.cost, "recipe from a vendor"))
            if (m := book.get(ls.item_id).market) is not None:
                options.append((m, "recipe on the AH"))
            if not ls.vendor and book.get(ls.item_id).market is None:
                unknown.append("recipe (drop, not on the AH)")
        else:
            unknown.append({"unknown": "source unknown"}.get(ls.kind, ls.kind))
    if options:
        cost, label = min(options)
        return cost, label
    return None, ", ".join(dict.fromkeys(unknown)) or "source unknown"


def evaluate_recipe(
    recipe: Recipe, book: PriceBook, ctx: CraftContext, opts: CraftOptions
) -> CraftEval:
    lines, cost, flags = [], 0.0, []
    mats_cap: float | None = None
    for item, qty in recipe.reagents:
        unit, source, listed = buy_price(item, book, ctx)
        lines.append(ReagentLine(item, _name(item, book, ctx), qty, unit, source, listed))
        if unit is None:
            cost = None
            flags.append(f"{_name(item, book, ctx)} isn't on the AH or sold by vendors")
            continue
        if cost is not None:
            cost += unit * qty
        if source == "AH":
            cap = listed / qty
            mats_cap = cap if mats_cap is None else min(mats_cap, cap)
    out_unit, on_ah = sell_unit(recipe.output, book, ctx)
    out = book.get(recipe.output)
    absorb = out.absorb_per_day if on_ah else 0.0
    if not on_ah:
        flags.append(
            "output sells to vendors only"
            if out.market is None
            else "output is worth more at a vendor"
        )
    if out.market is not None and not out.absorb_measured:
        flags.append("output demand estimated from listings")
    if on_ah:
        flags.extend(trend_flags(book, [recipe.output]))
    cooldown = 86400 / recipe.cooldown_s if recipe.cooldown_s else None
    if recipe.cooldown_s:
        flags.append(f"cooldown {recipe.cooldown_s / 86400:g} day(s)")
    if recipe.skill_estimated:
        flags.append("required skill estimated")
    learn_cost, learn_label = _learn(recipe, book)
    return CraftEval(
        recipe=recipe,
        output_name=_name(recipe.output, book, ctx),
        reagents=lines,
        cost=cost,
        output_unit=out_unit,
        output_on_ah=on_ah,
        output_sold_per_day=out.sold_per_day if out.absorb_measured else absorb or None,
        output_measured=out.absorb_measured,
        # vendors take any amount; the AH only its daily sales (your share of them)
        crafts_demand=(absorb * opts.market_share / recipe.output_qty) if on_ah else 1e9,
        crafts_mats=mats_cap,
        crafts_cooldown=cooldown,
        learn_cost=learn_cost,
        learn_label=learn_label,
        flags=flags,
    )


def craftable(recipe: Recipe, professions: dict[str, int]) -> bool:
    return professions.get(recipe.profession, 0) >= recipe.skill


def rank_crafts(
    recipes: list[Recipe],
    book: PriceBook,
    ctx: CraftContext,
    opts: CraftOptions,
    *,
    professions: dict[str, int] | None = None,
    min_sales: float = 0.0,
    cooldowns: bool = True,
) -> list[CraftEval]:
    """Profitable crafts, best profit per day first. With `professions`, only recipes those
    skills can make."""
    out = []
    for r in recipes:
        if professions is not None and not craftable(r, professions):
            continue
        if r.cooldown_s and not cooldowns:
            continue
        ev = evaluate_recipe(r, book, ctx, opts)
        if ev.profit is None or ev.profit <= 0:
            continue
        if ev.output_on_ah and (ev.output_sold_per_day or 0) < min_sales:
            continue
        out.append(ev)
    out.sort(key=lambda e: (-e.profit_per_day, -(e.profit or 0)))
    return out


# -- processing your own materials --------------------------------------------------------------


@dataclass
class Processing:
    item_id: int
    name: str
    raw_value: float  # selling the material itself (net, per unit)
    via: CraftEval
    value_per_unit: float  # crafted value per unit of this material, after other reagents
    units_per_day: float  # how many of these the recipe can absorb per day

    @property
    def uplift(self) -> float:
        return self.value_per_unit - self.raw_value


def processing_options(
    recipes: list[Recipe],
    book: PriceBook,
    ctx: CraftContext,
    opts: CraftOptions,
    professions: dict[str, int],
) -> dict[int, Processing]:
    """For each material, the best craft that turns it into more money than selling it."""
    best: dict[int, Processing] = {}
    for r in recipes:
        if not craftable(r, professions):
            continue
        ev = evaluate_recipe(r, book, ctx, opts)
        for line in ev.reagents:
            others = [ln for ln in ev.reagents if ln is not line]
            if any(ln.unit_cost is None for ln in others):
                continue
            other_cost = sum(ln.cost or 0 for ln in others)
            value = (ev.output_value - other_cost) / line.qty
            raw, _ = sell_unit(line.item_id, book, ctx)
            if value <= raw:
                continue
            caps = [ev.crafts_demand]
            if ev.crafts_cooldown is not None:
                caps.append(ev.crafts_cooldown)
            for ln in others:
                if ln.source == "AH":
                    caps.append(ln.listed / ln.qty)
            units = max(0.0, min(caps)) * line.qty
            if units <= 0:
                continue
            cur = best.get(line.item_id)
            if cur is None or value > cur.value_per_unit:
                best[line.item_id] = Processing(line.item_id, line.name, raw, ev, value, units)
    return best


def farm_craft_uplift(
    lines, processing: dict[int, Processing], *, hours: float, sell_days: float
) -> tuple[float, list[str]]:
    """Extra copper over a session from crafting a farm's yields before selling them.

    `lines` are the planner's ItemLines (produced units and the value they were given)."""
    extra, notes = 0.0, []
    for ln in lines:
        p = processing.get(ln.item_id)
        if p is None or ln.produced <= 0:
            continue
        units = min(ln.produced, p.units_per_day * sell_days)
        current = ln.value / ln.produced  # what the farm valued each unit at
        gain = units * (p.value_per_unit - current)
        if gain <= 0:
            continue
        extra += gain
        notes.append(
            f"{units:.0f} {ln.name} → {p.via.output_name} "
            f"({p.via.recipe.profession} {p.via.recipe.skill}): "
            f"{p.value_per_unit / 10000:.2f}g each instead of {current / 10000:.2f}g"
        )
    return extra / hours, notes
