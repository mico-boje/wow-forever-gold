"""Read-side queries over price_stats / price_daily / items."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ItemMatch:
    item_id: int
    name: str | None
    sell_price: int | None
    quality: str | None


@dataclass(frozen=True)
class HousePrice:
    house_id: int
    faction: str | None
    ts: str | None
    variants: int
    num_auctions: int
    quantity: int
    min_unit: int | None
    p25_unit: int | None
    median_unit: int | None
    market_unit: int | None
    sold_24h: int | None
    ambiguous_24h: int | None
    market_avg_24h: int | None
    market_avg_7d: int | None


def format_money(copper: int | None) -> str:
    if copper is None:
        return "-"
    g, rem = divmod(int(copper), 10000)
    s, c = divmod(rem, 100)
    if g:
        return f"{g:,}g {s:02d}s {c:02d}c"
    if s:
        return f"{s}s {c:02d}c"
    return f"{c}c"


def find_items(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[ItemMatch]:
    q = query.strip()
    if q.isdigit():
        row = conn.execute(
            "SELECT item_id, name, sell_price, quality FROM items WHERE item_id = ?", (int(q),)
        ).fetchone()
        return [ItemMatch(*row)] if row else [ItemMatch(int(q), None, None, None)]
    rows = conn.execute(
        """
        SELECT item_id, name, sell_price, quality FROM items
        WHERE name LIKE ? COLLATE NOCASE
        ORDER BY (lower(name) = lower(?)) DESC, length(name), item_id
        LIMIT ?
        """,
        (f"%{q}%", q, limit),
    ).fetchall()
    return [ItemMatch(*r) for r in rows]


def _latest_snapshot_ids(conn: sqlite3.Connection, region: str, house_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT MAX(id) FROM snapshots
        WHERE region = ? AND house_id = ? AND outcome = 'stored'
          AND ingested_at IS NOT NULL AND ingested_at != 'missing-file'
        GROUP BY kind
        """,
        (region, house_id),
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def house_price(
    conn: sqlite3.Connection,
    region: str,
    item_id: int,
    house_id: int,
    *,
    now: datetime | None = None,
) -> HousePrice | None:
    now = now or datetime.now(UTC)
    snap_ids = _latest_snapshot_ids(conn, region, house_id)
    if not snap_ids:
        return None
    marks = ",".join("?" * len(snap_ids))
    rows = conn.execute(
        f"""
        SELECT * FROM price_stats
        WHERE snapshot_id IN ({marks}) AND item_id = ? AND quantity > 0
        """,
        (*snap_ids, item_id),
    ).fetchall()
    faction = conn.execute(
        "SELECT faction FROM houses WHERE region = ? AND house_id = ?", (region, house_id)
    ).fetchone()

    since_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    since_7d = (now - timedelta(days=7)).isoformat(timespec="seconds")
    agg = conn.execute(
        """
        SELECT CAST(AVG(CASE WHEN ts >= ? AND market_unit > 0 THEN market_unit END) AS INTEGER),
               CAST(AVG(NULLIF(market_unit, 0)) AS INTEGER)
        FROM price_stats
        WHERE region = ? AND house_id = ? AND item_id = ? AND ts >= ?
        """,
        (since_24h, region, house_id, item_id, since_7d),
    ).fetchone()
    sold_24h = conn.execute(
        """
        SELECT SUM(sold_qty), SUM(ambiguous_qty) FROM price_stats
        WHERE region = ? AND house_id = ? AND item_id = ? AND ts >= ?
        """,
        (region, house_id, item_id, since_24h),
    ).fetchone()

    total_qty = sum(r["quantity"] for r in rows)
    market = (
        round(sum(r["market_unit"] * r["quantity"] for r in rows) / total_qty) if rows else None
    )
    return HousePrice(
        house_id=house_id,
        faction=faction[0] if faction else None,
        ts=max((r["ts"] for r in rows), default=None),
        variants=len(rows),
        num_auctions=sum(r["num_auctions"] for r in rows),
        quantity=total_qty,
        min_unit=min((r["min_unit"] for r in rows), default=None),
        # Percentiles only make sense for a single listing book (not mixed suffix variants).
        p25_unit=rows[0]["p25_unit"] if len(rows) == 1 else None,
        median_unit=rows[0]["median_unit"] if len(rows) == 1 else None,
        market_unit=market,
        sold_24h=sold_24h[0],
        ambiguous_24h=sold_24h[1],
        market_avg_24h=agg[0],
        market_avg_7d=agg[1],
    )
