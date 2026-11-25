"""Auction house resets: buy an item's cheap listings, relist near its normal price.

Per item on your house:

- **normal price:** the median of its hourly market price over the last week (what it usually
  trades around, not today's dip); lowered by a falling trend, and capped by an unlimited
  vendor offer;
- **the ladder:** every listing in the latest full snapshot, cheapest first;
- **sales and restocking:** units sold per day (measured), and units newly listed per day
  (from the hourly snapshots: what appeared beyond what was there before, net of sales).
  Fresh competition takes a share of the demand: you're assumed to get
  `sold / (sold + restocked)` of it;
- **the plan:** for each depth of buy-out (and within your budget), relist everything just
  under the next listing you didn't buy (the "wall"), or at the normal price if that's lower;
  count what sells within `horizon_days`, and value what's left at a discount. The depth with
  the best expected profit wins.

Deposits aren't counted (refunded when an auction sells), nor time.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from ..planner.pricing import PriceBook
from .stats import parse_listings

HISTORY_DAYS = 7
RESTOCK_DAYS = 3
MIN_HISTORY_HOURS = 12  # distinct hours of price history before a normal price counts
MIN_SOLD_UNITS = 3  # sales seen before the sales rate counts
MIN_SALES_HOURS = 6  # hours of usable snapshot pairs behind the sales rate
MAX_GAP_S = 3 * 3600  # restock is measured between snapshots this close
UNDERCUT = 0.99  # relist just under the wall
MIN_MARGIN = 1.15  # don't buy a listing unless the relist price is at least 15% above it
LEFTOVER_VALUE = 0.7  # unsold units, as a share of what they cost you
MIN_CAPTURE = 0.15
SKIP_FALLING_PER_DAY = -0.15  # don't reset into a market falling faster than this
MAX_RUNGS = 80


@dataclass
class Rung:
    price: int  # copper per unit
    qty: int


@dataclass
class ResetPlan:
    item_id: int
    name: str
    normal: int  # copper per unit
    relist: int
    units: int
    cost: int
    max_paid: int  # the dearest unit you buy
    wall: int | None  # cheapest listing left after the buy-out
    sold_per_day: float
    restock_per_day: float
    capture: float
    expected_sold: float
    revenue: float
    leftover_value: float
    profit: float
    history_hours: int
    ladder: list[Rung] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    budget_bound: bool = False  # the budget stopped the buy-out

    @property
    def roi(self) -> float:
        return self.profit / self.cost if self.cost else 0.0

    @property
    def days_to_sell(self) -> float | None:
        rate = self.sold_per_day * self.capture
        return self.units / rate if rate > 0 else None


def _latest_raw(conn: sqlite3.Connection, region: str, house_ids: list[int]) -> list[str]:
    marks = ",".join("?" * len(house_ids))
    rows = conn.execute(
        f"""SELECT path FROM snapshots WHERE id IN (
                SELECT MAX(id) FROM snapshots
                WHERE region = ? AND house_id IN ({marks}) AND outcome = 'stored'
                  AND path IS NOT NULL
                GROUP BY house_id, kind)""",
        (region, *house_ids),
    ).fetchall()
    return [r[0] for r in rows if r[0] and Path(r[0]).exists()]


def load_ladders(
    conn: sqlite3.Connection, region: str, house_ids: list[int]
) -> dict[int, list[Rung]]:
    """item -> listings cheapest first, merged by price. Gear with random suffixes (variants)
    is left out: its listings aren't interchangeable."""
    by_item: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for path in _latest_raw(conn, region, house_ids):
        for li in parse_listings(json.loads(gzip.decompress(Path(path).read_bytes()))):
            if li.variant or li.unit_price <= 0:
                continue
            by_item[li.item_id][li.unit_price] += li.quantity
    return {
        item: [Rung(p, q) for p, q in sorted(prices.items())][:MAX_RUNGS]
        for item, prices in by_item.items()
    }


@dataclass
class Flow:
    """What came and went over the measured snapshot pairs."""

    sold: float = 0.0  # units
    added: float = 0.0  # units newly listed
    seconds: float = 0.0  # time covered by usable snapshot pairs

    @property
    def sold_per_day(self) -> float:
        return self.sold * 86400 / self.seconds if self.seconds else 0.0

    @property
    def restock_per_day(self) -> float:
        return self.added * 86400 / self.seconds if self.seconds else 0.0


def history(
    conn: sqlite3.Connection, region: str, house_ids: list[int], item_ids, *, now: datetime
) -> tuple[dict[int, tuple[int, int]], dict[int, Flow]]:
    """(item -> (normal price, distinct hours), item -> sales and restocking over the last
    RESTOCK_DAYS, from snapshot pairs at most MAX_GAP_S apart)."""
    marks = ",".join("?" * len(house_ids))
    ids = sorted(item_ids)
    since = (now - timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds")
    restock_since = (now - timedelta(days=RESTOCK_DAYS)).isoformat(timespec="seconds")
    hourly: dict[int, dict[str, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    rows_by_house: dict[tuple[int, int], list] = defaultdict(list)
    for i in range(0, len(ids), 5000):
        chunk = ids[i : i + 5000]
        cmarks = ",".join("?" * len(chunk))
        for item, house, ts, qty, market, sold, interval in conn.execute(
            f"""SELECT item_id, house_id, ts, quantity, market_unit, sold_qty, interval_s
                FROM price_stats
                WHERE region = ? AND house_id IN ({marks}) AND item_id IN ({cmarks})
                  AND ts >= ?
                ORDER BY ts""",
            (region, *house_ids, *chunk, since),
        ):
            if qty > 0 and market > 0:
                hourly[item][ts[:13]].append((market, qty))
            if ts >= restock_since:
                rows_by_house[(item, house)].append((ts, qty, sold, interval))
    normal = {}
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")[:13]
    for item, hours in hourly.items():
        per_hour = {h: sum(m * q for m, q in v) / sum(q for _, q in v) for h, v in hours.items()}
        week = median(per_hour.values())
        recent = [p for h, p in per_hour.items() if h >= day_ago]
        # the lower of the week's and the last day's median: a price that fell is relisted
        # at what it fetches now, one that rose at what it usually fetches
        price = min(week, median(recent)) if recent else week
        normal[item] = (round(price), len(per_hour))
    flows: dict[int, Flow] = defaultdict(Flow)
    for (item, _house), rows in rows_by_house.items():
        for prev, cur in zip(rows, rows[1:], strict=False):
            gap = cur[3]
            if not gap or gap > MAX_GAP_S or cur[2] is None:
                continue
            f = flows[item]
            f.sold += cur[2]
            f.added += max(0, cur[1] - prev[1] + cur[2])
            f.seconds += gap
    return normal, dict(flows)


def _evaluate(
    ladder: list[Rung],
    normal: int,
    sold: float,
    capture: float,
    budget: int,
    horizon: float,
    cut: float,
) -> tuple | None:
    """Best (profit, units, cost, max_paid, relist, wall, expected_sold, revenue, leftover,
    budget_bound) over buy-out depths."""
    best = None
    units = cost = 0
    for k, rung in enumerate(ladder):
        wall = ladder[k + 1].price if k + 1 < len(ladder) else None
        relist = min(normal, int(wall * UNDERCUT)) if wall else normal
        if relist < rung.price * MIN_MARGIN:
            break  # this rung isn't cheap enough to be worth buying
        take, bound = rung.qty, False
        if cost + take * rung.price > budget:
            take, bound = (budget - cost) // rung.price, True
            if take <= 0:
                break
            wall = rung.price  # what you couldn't afford is still listed
            relist = min(normal, int(wall * UNDERCUT))
            if relist < rung.price * MIN_MARGIN:
                break
        units += take
        cost += take * rung.price
        expected = min(units, sold * capture * horizon)
        revenue = expected * relist * (1 - cut)
        leftover = (units - expected) * (cost / units) * LEFTOVER_VALUE
        profit = revenue + leftover - cost
        if best is None or profit > best[0]:
            best = (
                profit,
                units,
                cost,
                rung.price,
                relist,
                wall,
                expected,
                revenue,
                leftover,
                bound,
            )
        if bound:
            break
    return best


def plan_resets(
    conn: sqlite3.Connection,
    region: str,
    house_ids: list[int],
    book: PriceBook,
    budget: int,
    *,
    horizon_days: float = 3.0,
    now: datetime | None = None,
    vendor_caps: dict[int, float] | None = None,
) -> list[ResetPlan]:
    """Resets worth doing on these houses with `budget` copper, best expected profit first."""
    now = now or datetime.now(UTC)
    ladders = load_ladders(conn, region, house_ids)
    normal, flows = history(conn, region, house_ids, set(ladders), now=now)
    plans = []
    for item, ladder in ladders.items():
        p = book.get(item)
        flow = flows.get(item)
        if (
            item not in normal
            or not ladder
            or flow is None
            or flow.sold < MIN_SOLD_UNITS
            or flow.seconds < MIN_SALES_HOURS * 3600
        ):
            continue
        sold = flow.sold_per_day
        price, hours = normal[item]
        if hours < MIN_HISTORY_HOURS:
            continue
        flags = []
        if p.trend is not None and p.trend.trust and (p.trend.drift or 0) <= SKIP_FALLING_PER_DAY:
            continue  # buying into a crash
        if p.trend is not None and p.sell_factor < 0.995 and (p.trend.drift or 0) <= -0.01:
            price = round(price * p.sell_factor)
            flags.append(f"price falling {p.trend.drift * 100:+.0f}%/day: relist price lowered")
        if vendor_caps and item in vendor_caps and price > vendor_caps[item]:
            price = int(vendor_caps[item])
            flags.append("a vendor sells it: capped at the vendor's price")
        if p.vendor_buy and price > p.vendor_buy:
            price = p.vendor_buy
        new = flow.restock_per_day
        capture = max(MIN_CAPTURE, min(1.0, sold / (sold + new))) if sold + new > 0 else 1.0
        best = _evaluate(ladder, price, sold, capture, budget, horizon_days, book.cut)
        if best is None:
            continue
        profit, units, cost, max_paid, relist, wall, expected, revenue, leftover, bound = best
        if profit <= 0 or cost <= 0 or profit / cost < 0.1:
            continue
        plan = ResetPlan(
            item_id=item,
            name=p.name or f"item {item}",
            normal=normal[item][0],
            relist=relist,
            units=units,
            cost=cost,
            max_paid=max_paid,
            wall=wall,
            sold_per_day=sold,
            restock_per_day=new,
            capture=capture,
            expected_sold=expected,
            revenue=revenue,
            leftover_value=leftover,
            profit=profit,
            history_hours=hours,
            ladder=ladder[:12],
            flags=flags,
            budget_bound=bound,
        )
        if flow.seconds < 24 * 3600:
            plan.flags.append(f"sales measured over {flow.seconds / 3600:.0f} hours")
        if hours < 48:
            plan.flags.append(f"only {hours} hours of price history")
        if sold < 2:
            plan.flags.append(f"slow seller: {sold:.1f}/day")
        if new > sold:
            plan.flags.append(f"sellers relist faster ({new:.0f}/day) than it sells")
        if p.trend is not None and p.trend.swings:
            plan.flags.append(f"price swings ±{p.trend.swing * 100:.0f}%")
        days = plan.days_to_sell
        if days is not None and days > horizon_days:
            plan.flags.append(f"~{days:.0f} days to sell it all")
        if bound:
            plan.flags.append("your budget stops the buy-out early")
        plans.append(plan)
    plans.sort(key=lambda r: -r.profit)
    return plans
