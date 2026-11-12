"""Filter methods by what a character can do, value them at current prices, and rank.

Every method is scored as *expected gold over a play session of `hours`*, divided by
`hours`. Selling is capped by how much the market absorbs within `sell_days`
(times `market_share`, since other sellers exist); anything beyond that is valued at the
vendor price. That keeps "farm 500 Black Lotus" from looking like 500x the profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..methods.models import Faction, ItemQty, Kind, Method
from .pricing import ItemPrice, PriceBook, trend_flags
from .profile import CharacterProfile

Z90 = 1.2816  # z-score for the 10th/90th percentile range

# Better gear than a farm needs makes killing faster.
GEAR_BONUS_PER_TIER = 0.1


@dataclass(frozen=True)
class PlanOptions:
    hours: float = 2.0
    sell_days: float = 3.0
    market_share: float = 0.5
    allow_groups: bool = False


@dataclass
class ItemLine:
    item_id: int
    name: str
    produced: float  # units per session (gather) or used/made (craft)
    ah_units: float
    vendor_units: float
    value: int  # copper over the session (negative for reagents)


@dataclass
class Evaluation:
    method: Method
    gold_per_hour: float  # copper per hour, expected
    low: float  # copper per hour, ~10th percentile
    high: float  # copper per hour, ~90th percentile
    total: float  # copper over the session
    lines: list[ItemLine] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    time_used_h: float | None = None  # crafts: how long the crafting actually takes
    profit_per_craft: float | None = None
    crafts: int | None = None


@dataclass(frozen=True)
class Reason:
    kind: str  # level | profession | class | faction | gear | group
    text: str

    def __str__(self) -> str:
        return self.text


# Requirements you can work towards (as opposed to class/faction restrictions).
PROGRESSABLE = {"level", "profession", "gear"}


@dataclass
class Ineligible:
    method: Method
    reasons: list[Reason]

    @property
    def progressable(self) -> bool:
        return all(r.kind in PROGRESSABLE for r in self.reasons)


def eligibility(method: Method, profile: CharacterProfile, opts: PlanOptions) -> list[Reason]:
    req = method.requirements
    reasons = []
    if profile.level < req.level:
        reasons.append(Reason("level", f"level {req.level} (you are {profile.level})"))
    for prof, skill in req.professions.items():
        have = profile.professions.get(prof)
        if have is None:
            reasons.append(Reason("profession", f"{prof} {skill} (you don't have it)"))
        elif have < skill:
            reasons.append(Reason("profession", f"{prof} {skill} (you have {have})"))
    if req.classes and profile.cls not in req.classes:
        reasons.append(Reason("class", f"class: {', '.join(req.classes)}"))
    if req.faction != Faction.ANY and req.faction != profile.faction:
        reasons.append(Reason("faction", f"{req.faction} only"))
    if profile.gear.rank < req.gear.rank:
        reasons.append(Reason("gear", f"{req.gear} gear (you have {profile.gear})"))
    if req.group_size > 1 and not opts.allow_groups:
        reasons.append(Reason("group", f"group of {req.group_size}"))
    return reasons


def throughput_multiplier(method: Method, profile: CharacterProfile) -> float:
    mult = method.class_modifiers.get(profile.cls, 1.0)
    if method.kind == Kind.GATHER:
        mult *= profile.travel_multiplier
    if method.kind == Kind.FARM:
        mult *= 1 + GEAR_BONUS_PER_TIER * max(0, profile.gear.rank - method.requirements.gear.rank)
    return mult


def _capacity(price: ItemPrice, opts: PlanOptions) -> float:
    return price.absorb_per_day * opts.sell_days * opts.market_share


def _sell(produced: float, price: ItemPrice, opts: PlanOptions) -> tuple[float, float, int]:
    """Split produced units into AH sales (up to capacity) and vendor sales."""
    ah_net = price.ah_net
    if ah_net is None or ah_net <= price.vendor_sell:
        return 0.0, produced, round(produced * price.vendor_sell)
    ah_units = min(produced, _capacity(price, opts))
    vendor_units = produced - ah_units
    return ah_units, vendor_units, round(ah_units * ah_net + vendor_units * price.vendor_sell)


def _label(iq: ItemQty, price: ItemPrice) -> str:
    return price.name or iq.name


def evaluate_gathering(
    method: Method, profile: CharacterProfile, book: PriceBook, opts: PlanOptions
) -> Evaluation:
    mult = throughput_multiplier(method, profile)
    lines, flags = [], []
    total = low = high = 0.0
    for iq in method.yields:
        price = book.get(iq.item)
        produced = iq.qty * mult * opts.hours
        ah_units, vendor_units, value = _sell(produced, price, opts)
        lines.append(ItemLine(iq.item, _label(iq, price), produced, ah_units, vendor_units, value))
        total += value
        # Range: yield uncertainty only (prices are taken as given).
        sd = iq.sd * mult * opts.hours
        low += _sell(max(0.0, produced - Z90 * sd), price, opts)[2]
        high += _sell(produced + Z90 * sd, price, opts)[2]
        if price.market is None:
            flags.append(f"{_label(iq, price)}: not on the AH, valued at vendor price")
        elif vendor_units > 0.5 and price.ah_net and price.ah_net > price.vendor_sell:
            flags.append(
                f"{_label(iq, price)}: market takes ~{ah_units:.0f} of {produced:.0f}, "
                "rest valued at vendor"
            )
    vendor = method.vendor_copper_per_hour * mult * opts.hours
    if vendor:
        lines.append(ItemLine(0, "coins + vendor trash", 0, 0, 0, round(vendor)))
    total += vendor
    low += vendor * 0.8
    high += vendor * 1.2
    return Evaluation(
        method=method,
        gold_per_hour=total / opts.hours,
        low=low / opts.hours,
        high=high / opts.hours,
        total=total,
        lines=lines,
        flags=flags,
    )


def evaluate_craft(
    method: Method, profile: CharacterProfile, book: PriceBook, opts: PlanOptions
) -> Evaluation:
    flags = []
    unit_costs: dict[int, int] = {}
    reagent_limit = float("inf")
    for iq in method.reagents:
        price = book.get(iq.item)
        if iq.vendor and price.vendor_buy:
            unit_costs[iq.item] = price.vendor_buy
        elif price.market is not None:
            unit_costs[iq.item] = price.market
            reagent_limit = min(reagent_limit, price.listed_qty / iq.qty)
        else:
            flags.append(f"{_label(iq, price)}: no price available, can't evaluate")
            return Evaluation(method, 0, 0, 0, 0, flags=flags)
    cost_per_craft = sum(unit_costs[iq.item] * iq.qty for iq in method.reagents)

    # Crafts are limited by: time, how much output the market absorbs, reagents for sale.
    out_capacity = min(
        _capacity(book.get(o.item), opts) / o.qty if book.get(o.item).ah_net else float("inf")
        for o in method.outputs
    )
    by_time = method.crafts_per_hour * opts.hours
    crafts = int(min(by_time, out_capacity, reagent_limit))

    revenue_per_craft = 0.0
    for o in method.outputs:
        price = book.get(o.item)
        if price.market is None:
            flags.append(f"{_label(o, price)}: not on the AH, valued at vendor price")
        revenue_per_craft += o.qty * price.sell_value
    profit = revenue_per_craft - cost_per_craft
    if profit <= 0:
        flags.append("unprofitable at current prices")
        crafts = 0
    elif crafts == int(reagent_limit) and reagent_limit < min(by_time, out_capacity):
        flags.append(f"limited by reagents listed on the AH ({crafts} crafts)")
    elif crafts == int(out_capacity) and out_capacity < by_time:
        flags.append(f"limited by demand for the output (~{crafts} crafts)")

    lines = []
    for iq in method.reagents:
        used = iq.qty * crafts
        label = _label(iq, book.get(iq.item))
        lines.append(ItemLine(iq.item, label, used, 0, 0, -round(unit_costs[iq.item] * used)))
    for o in method.outputs:
        price = book.get(o.item)
        made = o.qty * crafts
        lines.append(
            ItemLine(o.item, _label(o, price), made, made, 0, round(made * price.sell_value))
        )
    total = max(0.0, profit) * crafts
    return Evaluation(
        method=method,
        gold_per_hour=total / opts.hours,
        low=total / opts.hours,
        high=total / opts.hours,
        total=total,
        lines=lines,
        flags=flags,
        time_used_h=crafts / method.crafts_per_hour if method.crafts_per_hour else None,
        profit_per_craft=profit,
        crafts=crafts,
    )


def evaluate(
    method: Method, profile: CharacterProfile, book: PriceBook, opts: PlanOptions
) -> Evaluation:
    if method.kind == Kind.CRAFT:
        ev = evaluate_craft(method, profile, book, opts)
    else:
        ev = evaluate_gathering(method, profile, book, opts)
    if method.kind != Kind.CRAFT:
        if method.calibration_hours > 0:
            ev.flags.append(
                f"yields calibrated from {method.calibration_sessions} session(s), "
                f"{method.calibration_hours:.1f} base-hours"
                + ("" if method.confidence == "measured" else " (under 3h: still settling)")
            )
        elif method.confidence == "estimate":
            ev.flags.append("yields are estimates (not yet measured in Forever)")
        elif method.confidence == "hypothesis":
            ev.flags.append("hypothesis from vanilla data: test it in game before relying on it")
    if any(not book.get(i).absorb_measured for i in method.item_ids if book.get(i).market):
        ev.flags.append("demand estimated from listings (no sales history yet)")
    sold_on_ah = [ln.item_id for ln in ev.lines if ln.item_id and ln.ah_units]
    ev.flags.extend(trend_flags(book, sold_on_ah))
    return ev


@dataclass
class Plan:
    ranked: list[Evaluation]
    ineligible: list[Ineligible]


def make_plan(
    methods: list[Method], profile: CharacterProfile, book: PriceBook, opts: PlanOptions
) -> Plan:
    ranked, ineligible = [], []
    for m in methods:
        reasons = eligibility(m, profile, opts)
        if reasons:
            ineligible.append(Ineligible(m, reasons))
        else:
            ranked.append(evaluate(m, profile, book, opts))
    ranked.sort(key=lambda e: e.gold_per_hour, reverse=True)
    return Plan(ranked=ranked, ineligible=ineligible)


def upgrade_targets(
    plan: Plan, profile: CharacterProfile, book: PriceBook, opts: PlanOptions, limit: int = 3
) -> list[tuple[Evaluation, list[Reason]]]:
    """Methods you can't do yet only because of level/profession skill, valued as if you
    could, and better than your current best. Useful as 'what to work towards'."""
    best = plan.ranked[0].gold_per_hour if plan.ranked else 0
    out = []
    for inel in plan.ineligible:
        if not inel.progressable or any(r.kind == "gear" for r in inel.reasons):
            continue
        m = inel.method
        boosted = profile.model_copy(
            update={
                "level": max(profile.level, m.requirements.level),
                "professions": {
                    **profile.professions,
                    **{
                        p: max(s, profile.professions.get(p, 0))
                        for p, s in m.requirements.professions.items()
                    },
                },
            }
        )
        ev = evaluate(m, boosted, book, opts)
        if ev.gold_per_hour > best:
            out.append((ev, inel.reasons))
    out.sort(key=lambda t: t[0].gold_per_hour, reverse=True)
    return out[:limit]
