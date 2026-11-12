"""Names for Forever items Blizzard's item API doesn't know.

1. From captures: page titles, and the item rows of Wowhead tables (drops, recipe lists,
   item lists). Free; refreshed whenever a page is captured.
2. From Wowhead's tooltip endpoint (nether.wowhead.com), the public API sites use to embed
   item tooltips. Slow and polite: one request a second, honest user agent, and every result
   is stored so an item is only ever asked for once.

Names go into the `items` table with source 'wowhead', only where Blizzard has no name, and
Blizzard's sync never overwrites them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .capture import capture_dir
from .sources import item_name_from_title, parse_raw_listviews

TOOLTIP_URL = "https://nether.wowhead.com/forever/tooltip/item/{item_id}"
USER_AGENT = "wowgold/0.1 (personal gold-making planner; tooltip lookups, 1 req/s)"
_ACTION = re.compile(r"^(Transmute|Smelt|Enchant|Create|Brew|Cook)\b|:")
QUALITY = {0: "POOR", 1: "COMMON", 2: "UNCOMMON", 3: "RARE", 4: "EPIC", 5: "LEGENDARY"}


@dataclass(frozen=True)
class ItemName:
    name: str
    quality: str | None = None
    sell_price: int | None = None


def _clean(name) -> str:
    return str(name or "").lstrip("0123456789").strip()


def _quality(row: dict) -> str | None:
    q = row.get("quality")
    if isinstance(q, int):
        return QUALITY.get(q)
    name = row.get("name")
    if isinstance(name, str) and name[:1].isdigit():
        return QUALITY.get(int(name[0]))  # Wowhead prefixes item names with their quality
    return None


def _item_rows(record: dict) -> list[list]:
    tables = [
        lv.get("data") or []
        for lv in (record.get("listviews") or {}).values()
        if isinstance(lv, dict)
    ]
    for script in record.get("raw") or []:
        tables += [rows for _, _, rows in parse_raw_listviews(script)]
    return tables


def captured_item_names(data_dir: Path) -> dict[int, ItemName]:
    """Item names found anywhere in your Wowhead captures."""
    names: dict[int, ItemName] = {}
    for path in sorted(capture_dir(data_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        kind, _, raw_id = path.stem.partition("-")
        title = item_name_from_title(str(record.get("title") or ""))
        if kind == "item" and raw_id.isdigit() and title:
            names.setdefault(int(raw_id), ItemName(title))
        for rows in _item_rows(record):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("creates"), list) and row["creates"]:
                    # recipe rows: the spell is usually named after what it makes
                    out, spell = row["creates"][0], _clean(row.get("name"))
                    # ...but not "Transmute: X", "Smelt X", "Enchant ..." (actions, not items)
                    if isinstance(out, int) and spell and not _ACTION.match(spell):
                        names.setdefault(out, ItemName(spell))
                    continue
                # item rows (drops, item lists): items have a class ("classs") or a slot
                is_item = ("classs" in row or "slot" in row) and isinstance(row.get("id"), int)
                if is_item and (n := _clean(row.get("name"))):
                    names[row["id"]] = ItemName(n, _quality(row))
    return names


def store_names(conn: sqlite3.Connection, names: dict[int, ItemName]) -> int:
    """Save names for items that have none yet. Returns how many were added."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    before = conn.total_changes
    for item_id, n in names.items():
        conn.execute(
            """
            INSERT INTO items (item_id, name, quality, sell_price, source, fetched_at)
            VALUES (?, ?, ?, ?, 'wowhead', ?)
            ON CONFLICT (item_id) DO UPDATE SET
                name = excluded.name,
                quality = COALESCE(items.quality, excluded.quality),
                sell_price = COALESCE(items.sell_price, excluded.sell_price),
                source = 'wowhead', fetched_at = excluded.fetched_at
            WHERE items.name IS NULL OR items.source = 'missing'
            """,
            (item_id, n.name, n.quality, n.sell_price, now),
        )
    conn.commit()
    return conn.total_changes - before


def unnamed_item_ids(conn: sqlite3.Connection, candidates: set[int]) -> list[int]:
    """Of `candidates`, the items with no name stored (and not already looked up)."""
    named = {
        r[0]
        for r in conn.execute(
            "SELECT item_id FROM items WHERE name IS NOT NULL OR source = 'wowhead-missing'"
        )
    }
    return sorted(i for i in candidates if i not in named)


_MONEY = re.compile(r'money(gold|silver|copper)">\s*(\d+)')


def parse_tooltip(payload: dict) -> ItemName | None:
    name = _clean(payload.get("name"))
    if not name:
        return None
    html = str(payload.get("tooltip") or "")
    sell = None
    if "Sell Price" in html:
        tail = html[html.index("Sell Price") :]
        parts = {k: int(v) for k, v in _MONEY.findall(tail[:600])}
        sell = parts.get("gold", 0) * 10000 + parts.get("silver", 0) * 100 + parts.get("copper", 0)
    q = payload.get("quality")
    return ItemName(name, QUALITY.get(q) if isinstance(q, int) else None, sell or None)


def fetch_tooltip_names(
    conn: sqlite3.Connection,
    item_ids: list[int],
    *,
    client: httpx.Client | None = None,
    delay_s: float = 1.0,
    on_progress: Callable[[int, int, str | None], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """Look up names one at a time. Returns (found, not found). Items Wowhead doesn't know
    are remembered as 'wowhead-missing' so they aren't asked for again."""
    own = client is None
    client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
    found = missing = 0
    try:
        for n, item_id in enumerate(item_ids, start=1):
            if should_stop and should_stop():
                break
            name = None
            try:
                resp = client.get(TOOLTIP_URL.format(item_id=item_id))
                if resp.status_code == 200:
                    name = parse_tooltip(resp.json())
                elif resp.status_code in (403, 429):
                    break  # being refused or rate limited: stop rather than push on
            except (httpx.HTTPError, ValueError):
                name = None
            if name:
                store_names(conn, {item_id: name})
                found += 1
            else:
                conn.execute(
                    """
                    INSERT INTO items (item_id, source, fetched_at) VALUES (?, 'wowhead-missing', ?)
                    ON CONFLICT (item_id) DO UPDATE SET source = 'wowhead-missing',
                        fetched_at = excluded.fetched_at
                    WHERE items.name IS NULL
                    """,
                    (item_id, datetime.now(UTC).isoformat(timespec="seconds")),
                )
                conn.commit()
                missing += 1
            if on_progress:
                on_progress(n, len(item_ids), name.name if name else None)
            if n < len(item_ids) and delay_s:
                time.sleep(delay_s)
    finally:
        if own:
            client.close()
    return found, missing
