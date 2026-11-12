"""When each item first showed up on the auction house, to flag new arrivals.

"New" means first seen within the last few days *and* after the collector's first hours: the
first snapshots see everything for the first time, which says nothing about the item.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

NEW_DAYS = 7
WARMUP = timedelta(hours=6)


def collection_start(conn: sqlite3.Connection, region: str) -> datetime | None:
    row = conn.execute(
        "SELECT MIN(fetched_at) FROM snapshots WHERE region = ? AND outcome = 'stored'", (region,)
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def first_seen(conn: sqlite3.Connection, region: str) -> dict[int, str]:
    """item -> earliest time it was listed (hourly stats, or daily rollups for older data)."""
    out: dict[int, str] = {}
    for item_id, ts in conn.execute(
        "SELECT item_id, MIN(ts) FROM price_stats WHERE region = ? AND quantity > 0 "
        "GROUP BY item_id",
        (region,),
    ):
        out[item_id] = ts
    for item_id, day in conn.execute(
        "SELECT item_id, MIN(day) FROM price_daily WHERE region = ? GROUP BY item_id", (region,)
    ):
        stamp = f"{day}T00:00:00+00:00"
        if item_id not in out or stamp < out[item_id]:
            out[item_id] = stamp
    return out


def new_arrivals(
    conn: sqlite3.Connection, region: str, *, days: int = NEW_DAYS, now: datetime | None = None
) -> dict[int, str]:
    """Items first seen in the last `days` days (and after the collector's warm-up)."""
    start = collection_start(conn, region)
    if start is None:
        return {}
    now = now or datetime.now(UTC)
    since = max(start + WARMUP, now - timedelta(days=days)).isoformat(timespec="seconds")
    return {i: ts for i, ts in first_seen(conn, region).items() if ts >= since}
