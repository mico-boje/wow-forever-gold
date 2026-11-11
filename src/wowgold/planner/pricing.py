"""Price book: what an item is worth, and how much of it the market absorbs, in the
auction houses a plan is made for."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

AH_CUT = 0.05  # auction house fee on successful sales

# Without measured sales we guess daily demand as a fraction of what's listed. Crude, but
# it scales with how big the market for an item is; replaced by real sales once the
# collector has run for ~a day.
LISTED_FRACTION_SOLD_PER_DAY = 0.25
# Minimum hours of sales data before we trust the measured daily volume.
MIN_SALES_COVERAGE_H = 12


@dataclass(frozen=True)
class ItemPrice:
    item_id: int
    name: str | None
    market: int | None  # copper per unit (AH), None = not listed
    listed_qty: int
    vendor_sell: int  # what a vendor pays you
    vendor_buy: int | None  # what a vendor charges (purchase_price), if sold by vendors
    sold_per_day: float | None  # measured; None = no sales data
    absorb_per_day: float  # measured or estimated units/day the market takes
    absorb_measured: bool

    @property
    def ah_net(self) -> int | None:
        return round(self.market * (1 - AH_CUT)) if self.market else None

    @property
    def sell_value(self) -> int:
        """Best per-unit value when selling one more unit."""
        return max(self.ah_net or 0, self.vendor_sell)


@dataclass
class PriceBook:
    house_ids: list[int]
    prices: dict[int, ItemPrice] = field(default_factory=dict)
    sales_coverage_h: float = 0.0

    def get(self, item_id: int) -> ItemPrice:
        return self.prices.get(item_id) or ItemPrice(
            item_id, None, None, 0, 0, None, None, 0.0, False
        )


def _latest_snapshot_ids(conn: sqlite3.Connection, region: str, house_ids: list[int]) -> list[int]:
    marks = ",".join("?" * len(house_ids))
    rows = conn.execute(
        f"""
        SELECT MAX(id) FROM snapshots
        WHERE region = ? AND house_id IN ({marks}) AND outcome = 'stored'
          AND ingested_at IS NOT NULL AND ingested_at != 'missing-file'
        GROUP BY house_id, kind
        """,
        (region, *house_ids),
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def build_price_book(
    conn: sqlite3.Connection,
    region: str,
    house_ids: list[int],
    item_ids: set[int],
    *,
    now: datetime | None = None,
) -> PriceBook:
    """Combine the latest snapshot of each house. With several houses, quantities add up
    and the market price is the quantity-weighted average."""
    now = now or datetime.now(UTC)
    book = PriceBook(house_ids=list(house_ids))
    if not house_ids or not item_ids:
        return book
    snap_ids = _latest_snapshot_ids(conn, region, house_ids)
    ids = sorted(item_ids)
    item_marks = ",".join("?" * len(ids))

    current: dict[int, tuple[int, int]] = {}  # item -> (sum(market*qty), qty)
    if snap_ids:
        snap_marks = ",".join("?" * len(snap_ids))
        for item_id, weighted, qty in conn.execute(
            f"""
            SELECT item_id, SUM(market_unit * quantity), SUM(quantity) FROM price_stats
            WHERE snapshot_id IN ({snap_marks}) AND item_id IN ({item_marks}) AND quantity > 0
            GROUP BY item_id
            """,
            (*snap_ids, *ids),
        ):
            current[item_id] = (weighted, qty)

    # Sales over the last 24h; coverage = span of snapshots that carried a sales diff.
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    house_marks = ",".join("?" * len(house_ids))
    cov = conn.execute(
        f"""
        SELECT COALESCE(SUM(interval_s), 0) FROM (
            SELECT DISTINCT snapshot_id, house_id, interval_s FROM price_stats
            WHERE region = ? AND house_id IN ({house_marks}) AND ts >= ? AND interval_s IS NOT NULL
        )
        """,
        (region, *house_ids, since),
    ).fetchone()[0]
    # Coverage per house (averaged), in hours.
    book.sales_coverage_h = cov / 3600 / max(len(house_ids), 1)
    sold: dict[int, int] = {}
    if book.sales_coverage_h > 0:
        for item_id, qty in conn.execute(
            f"""
            SELECT item_id, SUM(sold_qty) FROM price_stats
            WHERE region = ? AND house_id IN ({house_marks}) AND item_id IN ({item_marks})
              AND ts >= ? AND sold_qty IS NOT NULL
            GROUP BY item_id
            """,
            (region, *house_ids, *ids, since),
        ):
            sold[item_id] = qty or 0

    meta = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT item_id, name, sell_price, purchase_price FROM items "
            f"WHERE item_id IN ({item_marks})",
            ids,
        )
    }
    measured = book.sales_coverage_h >= MIN_SALES_COVERAGE_H
    for item_id in ids:
        weighted, qty = current.get(item_id, (0, 0))
        name, sell, buy = meta.get(item_id, (None, None, None))
        sold_per_day = (
            sold.get(item_id, 0) * 24 / book.sales_coverage_h if book.sales_coverage_h else None
        )
        absorb = (
            sold_per_day
            if measured and sold_per_day is not None
            else (qty * LISTED_FRACTION_SOLD_PER_DAY)
        )
        book.prices[item_id] = ItemPrice(
            item_id=item_id,
            name=name,
            market=round(weighted / qty) if qty else None,
            listed_qty=qty,
            vendor_sell=sell or 0,
            vendor_buy=buy or None,
            sold_per_day=sold_per_day,
            absorb_per_day=absorb,
            absorb_measured=measured and sold_per_day is not None,
        )
    return book


def pick_houses(
    conn: sqlite3.Connection,
    region: str,
    faction: str,
    explicit: list[int] | None = None,
    configured: list[int] | None = None,
) -> list[int]:
    """Houses to price with: explicit choice, else WOWGOLD_HOUSES, else the busiest house of
    the faction."""
    if explicit:
        return list(explicit)
    if configured:
        return list(configured)
    from .. import db

    candidates = [
        r
        for r in db.list_houses(conn, region)
        if r["faction"] == faction and (r["last_auctions"] or r["last_commodities"])
    ]
    if not candidates:
        return []
    best = max(candidates, key=lambda r: (r["last_auctions"] or 0) + (r["last_commodities"] or 0))
    return [best["house_id"]]
