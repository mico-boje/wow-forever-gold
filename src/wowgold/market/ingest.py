"""Turn stored raw snapshots into price_stats rows, and prune/roll up old data."""

from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stats import Listing, aggregate, estimate_sales, parse_listings

# Don't diff snapshots further apart than this: too many listings could have expired.
MAX_SALES_INTERVAL_S = 3 * 3600


@dataclass
class IngestSummary:
    snapshots: int = 0
    rows: int = 0
    skipped: int = 0


def _load(path: str | None) -> list[Listing] | None:
    if not path or not Path(path).exists():
        return None
    return parse_listings(json.loads(gzip.decompress(Path(path).read_bytes())))


def _seconds_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def _sales_cols(sl, interval: float | None) -> tuple[int | None, int | None, int | None]:
    if interval is None:
        return None, None, None  # no usable previous snapshot: sales unknown, not zero
    if sl is None:
        return 0, 0, int(interval)
    return sl.sold_qty, sl.ambiguous_qty, int(interval)


def ingest_pending(conn: sqlite3.Connection) -> IngestSummary:
    """Parse every stored-but-not-ingested snapshot, oldest first."""
    summary = IngestSummary()
    pending = conn.execute(
        """
        SELECT * FROM snapshots
        WHERE outcome = 'stored' AND ingested_at IS NULL
        ORDER BY fetched_at, id
        """
    ).fetchall()
    for snap in pending:
        listings = _load(snap["path"])
        if listings is None:
            summary.skipped += 1
            conn.execute(
                "UPDATE snapshots SET ingested_at = ? WHERE id = ?", ("missing-file", snap["id"])
            )
            continue

        prev = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE region = ? AND house_id = ? AND kind = ? AND outcome = 'stored' AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (snap["region"], snap["house_id"], snap["kind"], snap["id"]),
        ).fetchone()
        sales, interval = {}, None
        if prev is not None:
            interval = _seconds_between(prev["fetched_at"], snap["fetched_at"])
            prev_listings = _load(prev["path"]) if interval <= MAX_SALES_INTERVAL_S else None
            if prev_listings is not None:
                sales = estimate_sales(prev_listings, listings, interval)
            else:
                interval = None

        stats = aggregate(listings)
        rows = []
        for key in stats.keys() | sales.keys():
            item_id, variant = key
            st = stats.get(key)
            sl = sales.get(key)
            if st is None:
                # Item sold out completely: record the sale with an empty book.
                if sl is None or not (sl.sold_qty or sl.ambiguous_qty):
                    continue
                st_vals = (0, 0, 0, 0, 0, 0, 0)
            else:
                st_vals = (
                    st.num_auctions,
                    st.quantity,
                    st.min_unit,
                    st.p10_unit,
                    st.p25_unit,
                    st.median_unit,
                    st.market_unit,
                )
            rows.append(
                (
                    snap["id"],
                    snap["region"],
                    snap["house_id"],
                    snap["kind"],
                    snap["fetched_at"],
                    item_id,
                    variant,
                    *st_vals,
                    *_sales_cols(sl, interval),
                )
            )
        conn.executemany(
            "INSERT OR REPLACE INTO price_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.execute(
            "UPDATE snapshots SET ingested_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(timespec="seconds"), snap["id"]),
        )
        conn.commit()
        summary.snapshots += 1
        summary.rows += len(rows)
    return summary


@dataclass
class PruneSummary:
    raw_files_deleted: int = 0
    hourly_rows_rolled_up: int = 0


def prune(
    conn: sqlite3.Connection, *, keep_raw: int = 2, keep_hourly_days: int = 14
) -> PruneSummary:
    """Delete old raw files (keeping the newest `keep_raw` per house/kind for sales diffs)
    and roll hourly price_stats older than `keep_hourly_days` into price_daily."""
    summary = PruneSummary()
    rows = conn.execute(
        """
        SELECT id, path FROM (
            SELECT id, path, ingested_at,
                   ROW_NUMBER() OVER (PARTITION BY region, house_id, kind ORDER BY id DESC) AS rn
            FROM snapshots WHERE outcome = 'stored' AND path IS NOT NULL
        ) WHERE rn > ? AND ingested_at IS NOT NULL
        """,
        (keep_raw,),
    ).fetchall()
    for r in rows:
        Path(r["path"]).unlink(missing_ok=True)
        conn.execute("UPDATE snapshots SET path = NULL WHERE id = ?", (r["id"],))
        summary.raw_files_deleted += 1

    cutoff = (datetime.now(UTC) - timedelta(days=keep_hourly_days)).date().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO price_daily
        SELECT region, house_id, kind, item_id, variant, substr(ts, 1, 10) AS day,
               COUNT(*), COALESCE(CAST(AVG(NULLIF(market_unit, 0)) AS INTEGER), 0),
               COALESCE(MIN(NULLIF(min_unit, 0)), 0),
               CAST(AVG(quantity) AS INTEGER), SUM(sold_qty)
        FROM price_stats
        WHERE substr(ts, 1, 10) < ?
        GROUP BY region, house_id, kind, item_id, variant, day
        """,
        (cutoff,),
    )
    cur = conn.execute("DELETE FROM price_stats WHERE substr(ts, 1, 10) < ?", (cutoff,))
    summary.hourly_rows_rolled_up = cur.rowcount
    conn.commit()
    return summary
