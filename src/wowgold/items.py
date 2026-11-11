"""Item metadata: backfill from Blizzard's static API, with a manual override file.

At launch roughly 40% of auctioned Forever item IDs 404 on the Forever static item
endpoint, mostly original vanilla items. Lookup order:
  1. static-forever-{region}            -> source 'blizzard'
  2. static-classic1x-{region} (Era)    -> source 'classic_era' (same vanilla item IDs;
     Forever may have changed stats, but names and vendor prices are a good fallback)
  3. otherwise                          -> source 'missing' (retried after a few days)
Names can also be supplied in data/item_overrides.csv (columns: item_id,name[,quality]).
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .blizzard import BlizzardClient
from .blizzard.namespaces import classic_era, static

RETRY_MISSING_AFTER = timedelta(days=3)


@dataclass
class ItemSyncSummary:
    requested: int = 0
    found: int = 0
    classic_era: int = 0
    missing: int = 0
    overrides: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def item_ids_needing_sync(conn: sqlite3.Connection, *, retry_missing: bool = False) -> list[int]:
    retry_after = timedelta(0) if retry_missing else RETRY_MISSING_AFTER
    retry_before = (datetime.now(UTC) - retry_after).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT DISTINCT ps.item_id FROM price_stats ps
        LEFT JOIN items i ON i.item_id = ps.item_id
        WHERE i.item_id IS NULL OR (i.source = 'missing' AND i.fetched_at < ?)
        ORDER BY ps.item_id
        """,
        (retry_before,),
    ).fetchall()
    return [r[0] for r in rows]


def _name(obj: dict | None) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def item_row(item_id: int, payload: dict | None, source: str = "blizzard") -> tuple:
    if payload is None:
        return (item_id, None, None, None, None, None, None, None, None, None, "missing", _now())
    return (
        item_id,
        payload.get("name"),
        (payload.get("quality") or {}).get("type"),
        _name(payload.get("item_class")),
        _name(payload.get("item_subclass")),
        payload.get("level"),
        payload.get("required_level"),
        payload.get("sell_price"),
        payload.get("purchase_price"),
        int(bool(payload.get("is_stackable"))),
        source,
        _now(),
    )


def sync_items(
    conn: sqlite3.Connection,
    client: BlizzardClient,
    *,
    item_ids: list[int] | None = None,
    workers: int = 8,
    retry_missing: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> ItemSyncSummary:
    ids = (
        item_ids
        if item_ids is not None
        else item_ids_needing_sync(conn, retry_missing=retry_missing)
    )
    summary = ItemSyncSummary(requested=len(ids))
    if not ids:
        return summary
    sources = ((static(client.region), "blizzard"), (classic_era(client.region), "classic_era"))
    client._ensure_token()  # fetch once before fanning out to threads

    def fetch(item_id: int) -> tuple[int, dict | None, str]:
        for ns, source in sources:
            resp = client.get(f"/data/wow/item/{item_id}", ns)
            if resp.ok:
                return item_id, resp.json(), source
        return item_id, None, "missing"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (item_id, payload, source) in enumerate(pool.map(fetch, ids), start=1):
            row = item_row(item_id, payload, source)
            # Never clobber a manual override with a 'missing' marker.
            conn.execute(
                """
                INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (item_id) DO UPDATE SET
                    name=excluded.name, quality=excluded.quality, item_class=excluded.item_class,
                    item_subclass=excluded.item_subclass, level=excluded.level,
                    required_level=excluded.required_level, sell_price=excluded.sell_price,
                    purchase_price=excluded.purchase_price, is_stackable=excluded.is_stackable,
                    source=excluded.source, fetched_at=excluded.fetched_at
                WHERE items.source != 'override' OR excluded.source = 'blizzard'
                """,
                row,
            )
            if payload is None:
                summary.missing += 1
            elif source == "classic_era":
                summary.classic_era += 1
            else:
                summary.found += 1
            if n % 200 == 0:
                conn.commit()
                if on_progress:
                    on_progress(n, len(ids))
    conn.commit()
    return summary


def load_overrides(conn: sqlite3.Connection, path: Path) -> int:
    """Apply manual names for items the API doesn't know. Blizzard data wins if present."""
    if not path.exists():
        return 0
    n = 0
    with path.open(newline="") as fh:
        for rec in csv.DictReader(fh):
            if not rec.get("item_id") or not rec.get("name"):
                continue
            conn.execute(
                """
                INSERT INTO items (item_id, name, quality, source, fetched_at)
                VALUES (?, ?, ?, 'override', ?)
                ON CONFLICT (item_id) DO UPDATE SET
                    name = excluded.name,
                    quality = COALESCE(excluded.quality, items.quality),
                    source = 'override'
                WHERE items.source != 'blizzard'
                """,
                (int(rec["item_id"]), rec["name"].strip(), rec.get("quality") or None, _now()),
            )
            n += 1
    conn.commit()
    return n
