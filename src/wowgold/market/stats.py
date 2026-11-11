"""Pure functions: raw auction payloads -> per-item price statistics and sales estimates.

Observed data shapes (EU, Nov 2026):
- auctions:    {id, item:{id, bonus_lists?, modifiers?}, buyout, bid?, quantity(=1), time_left}
- commodities: {id, item:{id}, unit_price, quantity, time_left}
Prices are copper. Regular auctions all had quantity 1, but we still divide buyout by
quantity in case stacked listings appear.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

# Fraction of the cheapest units averaged into the "market" price. Taking the low end
# ignores absurd high listings; averaging over a band (not just the minimum) resists a
# single undercut.
MARKET_FRACTION = 0.25

# time_left buckets: SHORT <30m, MEDIUM 30m-2h, LONG 2-12h, VERY_LONG 12-48h.
# Minimum remaining seconds per bucket: a listing that vanished within less than this
# cannot have expired, so it was bought (or cancelled).
MIN_REMAINING_S = {"SHORT": 0, "MEDIUM": 30 * 60, "LONG": 2 * 3600, "VERY_LONG": 12 * 3600}

ItemKey = tuple[int, str]  # (item_id, variant)


@dataclass(frozen=True, slots=True)
class Listing:
    auction_id: int
    item_id: int
    variant: str
    unit_price: int
    quantity: int
    time_left: str


@dataclass(frozen=True, slots=True)
class ItemStats:
    num_auctions: int
    quantity: int
    min_unit: int
    p10_unit: int
    p25_unit: int
    median_unit: int
    market_unit: int


@dataclass(frozen=True, slots=True)
class SalesEstimate:
    sold_qty: int
    ambiguous_qty: int


def variant_of(item: dict) -> str:
    bonus = item.get("bonus_lists")
    return ",".join(str(b) for b in sorted(bonus)) if bonus else ""


def parse_listings(payload: dict) -> list[Listing]:
    out = []
    for a in payload.get("auctions") or []:
        qty = int(a.get("quantity") or 1)
        if "unit_price" in a:
            unit = int(a["unit_price"])
        elif "buyout" in a:
            unit = int(a["buyout"]) // max(qty, 1)
        else:
            continue  # bid-only auction: no usable price
        item = a.get("item") or {}
        out.append(
            Listing(
                auction_id=int(a["id"]),
                item_id=int(item["id"]),
                variant=variant_of(item),
                unit_price=unit,
                quantity=qty,
                time_left=a.get("time_left", ""),
            )
        )
    return out


def _weighted_percentile(sorted_pairs: list[tuple[int, int]], total: int, q: float) -> int:
    """Price at which `q` of the units (by quantity) are at or below."""
    target = max(1, q * total)
    seen = 0
    for price, qty in sorted_pairs:
        seen += qty
        if seen >= target:
            return price
    return sorted_pairs[-1][0]


def summarize(pairs: Iterable[tuple[int, int]]) -> ItemStats:
    """Stats from (unit_price, quantity) pairs."""
    sorted_pairs = sorted(pairs)
    total = sum(q for _, q in sorted_pairs)
    band = max(1, round(total * MARKET_FRACTION))
    taken = acc = 0
    for price, qty in sorted_pairs:
        use = min(qty, band - taken)
        acc += price * use
        taken += use
        if taken >= band:
            break
    return ItemStats(
        num_auctions=len(sorted_pairs),
        quantity=total,
        min_unit=sorted_pairs[0][0],
        p10_unit=_weighted_percentile(sorted_pairs, total, 0.10),
        p25_unit=_weighted_percentile(sorted_pairs, total, 0.25),
        median_unit=_weighted_percentile(sorted_pairs, total, 0.50),
        market_unit=round(acc / taken),
    )


def aggregate(listings: Iterable[Listing]) -> dict[ItemKey, ItemStats]:
    groups: dict[ItemKey, list[tuple[int, int]]] = defaultdict(list)
    for li in listings:
        groups[(li.item_id, li.variant)].append((li.unit_price, li.quantity))
    return {key: summarize(pairs) for key, pairs in groups.items()}


def estimate_sales(
    previous: Iterable[Listing], current: Iterable[Listing], interval_s: float
) -> dict[ItemKey, SalesEstimate]:
    """Compare two snapshots of the same house.

    - Listing still present with lower quantity -> the difference sold (commodity partial buy).
    - Listing gone, and it had enough time left that it could not have expired -> sold
      (or cancelled; the API can't tell those apart).
    - Listing gone but could have expired in the interval -> ambiguous.
    """
    cur = {li.auction_id: li for li in current}
    sold: dict[ItemKey, int] = defaultdict(int)
    ambiguous: dict[ItemKey, int] = defaultdict(int)
    for li in previous:
        key = (li.item_id, li.variant)
        now = cur.get(li.auction_id)
        if now is not None:
            if now.quantity < li.quantity:
                sold[key] += li.quantity - now.quantity
            continue
        if MIN_REMAINING_S.get(li.time_left, 0) < interval_s:
            ambiguous[key] += li.quantity
        else:
            sold[key] += li.quantity
    keys = set(sold) | set(ambiguous)
    return {
        k: SalesEstimate(sold_qty=sold.get(k, 0), ambiguous_qty=ambiguous.get(k, 0)) for k in keys
    }
